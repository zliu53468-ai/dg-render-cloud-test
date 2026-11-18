#!/usr/bin/env python3
# coding: utf-8
"""DG local, visible-screen observer V6: current TABLE + local SHOE SEGMENTS.

This is a read-only visual diagnostic tool. It does not log in, bet, spoof activity,
reconnect around access controls, read auth tokens, or claim official shoe/round IDs.
Automatic shoe boundaries are VISUAL INFERENCES from a reset of the bead-road grid;
all score candidates are UNVERIFIED. Unknown state stops the recorder (fail-closed).
"""
import argparse
import csv
import json
import os
import re
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import dg_rb02_v6 as dg
import score_engine_v3 as score

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent if (ROOT.parent / 'RB02_REGION.json').exists() else ROOT
OUTPUT = PROJECT / 'DG_CURRENT_SHOE_V6_OUTPUT'
ROAD_FILE = PROJECT / 'DG_V5_BEAD_ROAD_REGION.json'
SAMPLES = ROOT / 'samples'
CSV_FIELDS = ['candidate_id','table','table_detected','dealer','local_shoe_segment',
              'shoe_boundary_reason','shoe_id_official','player_points','banker_points',
              'winner','score_seen_at','saved_at','capture_delay_ms','timer_last_seen',
              'verified','official_round_id','official_settled_at','agreement_samples',
              'evidence_image','warning']


def clock():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def log_event(output, kind, **fields):
    dg.append_jsonl(Path(output) / 'DG_V6_EVENTS.jsonl', {
        'type':kind,'at':clock(),**fields
    })


def load_road_region():
    if not ROAD_FILE.is_file():
        raise RuntimeError('Missing DG_V5_BEAD_ROAD_REGION.json. Run 01_CALIBRATE_ROAD.cmd once.')
    cfg=json.loads(ROAD_FILE.read_text(encoding='utf-8'))
    reg=cfg.get('road')
    if not dg.region_ok(reg) or reg['height'] < 65 or reg['width'] < 75:
        raise RuntimeError('Invalid bead-road region. Recalibrate the six-row grid.')
    return reg


def score_region():
    return dg.load_score_region()


def v4_regions():
    r=dg.get_regions()
    r['road']=load_road_region()
    return r


def decode_png(path):
    data=np.frombuffer(Path(path).read_bytes(),dtype=np.uint8)
    img=cv2.imdecode(data,cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError('Unable to decode sample: '+str(path))
    return img


def marker_stats(road_img):
    """Estimate colored bead markers in a *tightly selected six-row bead grid*.

    Returns {'markers': int, 'neutral': float} or None if the crop is not a
    plausible bright grid. This is NOT an official round or shoe counter.
    """
    if road_img is None or road_img.size==0:
        return None
    h,w=road_img.shape[:2]
    if h < 65 or w < 75 or not 0.6 <= w/h <= 12:
        return None
    hsv=cv2.cvtColor(road_img,cv2.COLOR_BGR2HSV)
    H,S,V=cv2.split(hsv)
    # A covered/dark browser or non-road visual must not be interpreted as
    # zero rounds (which could otherwise cause a false shoe reset).
    neutral=float(np.mean((S<52)&(V>142)))
    if neutral < 0.25:
        return None
    colorful=(S>92)&(V>75)&(((H<=13)|(H>=168))|
                                 ((H>=88)&(H<=141))|
                                 ((H>=30)&(H<=89)))
    mask=(colorful.astype(np.uint8)*255)
    # Prevent tiny disconnected text strokes from being counted as beads.
    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((2,2),np.uint8))
    cell=h/6.0
    nc,_,stats,_=cv2.connectedComponentsWithStats(mask,connectivity=8)
    count=0
    for i in range(1,nc):
        _,_,cw,ch,area=(int(x) for x in stats[i])
        if 0.45*cell <= cw <= 1.42*cell and 0.45*cell <= ch <= 1.42*cell \
           and area >= max(13,0.14*cell*cell):
            count+=1
    return {'markers':count, 'neutral':round(neutral,3)}


def normalize_current_table(value):
    # An OCR S is ambiguous: on the user's real RB03 screenshot, RBOS was
    # previously mapped to RB05. Do not guess a different table number.
    return dg.normalize_table(str(value or ''))


def parse_live_header(crop):
    return dg.parse_header(crop)


class ShoeTracker:
    """Tracks local segments, NOT vendor-issued shoe IDs.

    A clear of the bead-road history is only promoted to a *suspected visual
    new shoe* after repeated valid screen observations plus fresh table & timer.
    Temporary overlays, tab switches, and missing pixels do not trigger a reset.
    """
    def __init__(self, output, *, min_prior_markers=12, drop_ratio=0.35, low_required=3):
        self.output=Path(output)
        self.table=None
        self.dealer=None
        self.dealer_tick=-1e12
        self.table_history=deque(maxlen=6)
        self.dealer_history=deque(maxlen=5)
        self.table_unconfirmed=False  # unsafe to attribute scores to previous table
        self.pending_table=None
        self.table_ocr_failures=0
        self.table_tick=-1e12
        self.timer=None
        self.timer_tick=-1e12
        self.timer_visual_tick=-1e12
        self.timer_at=None
        self.timer_last_logged=None
        self.road_tick=-1e12
        self.road_count=None
        self.road_neutral=None
        self.peak=0
        self.low_hits=0
        self.low_since=None
        self.clear_growth=0
        self.clear_growth_last=None
        self.clear_growth_tick=None
        self.big_drop_noted=False
        self.timer_last_change_tick=-1e12
        self.timer_raw_history=deque(maxlen=4)
        self.timer_pending_rollover=None
        self.timer_frozen_value=None
        self.min_prior_markers=min_prior_markers
        self.drop_ratio=drop_ratio
        self.low_required=low_required
        self.session=uuid.uuid4().hex[:8]
        self.index=0
        self.segment=None
        self.segment_reason=None
        self.reset_pause_until=-1e12
        self.status='WAITING_FOR_TABLE'
        self.last_status=None
        self.last_status_tick=-1e12
        self.last_header_result=None
        self.changed_epoch=0
        self.invalidation_needed=False

    def new_segment(self,table,reason,tick,*,prior_peak=None):
        if self.table != table:
            # Avoid attaching the previous table's dealer or countdown to a new table.
            self.dealer=None
            self.dealer_tick=-1e12
            self.dealer_history.clear()
            self.timer=None
            self.timer_tick=-1e12
            self.timer_visual_tick=-1e12
            self.timer_at=None
            self.timer_last_logged=None
        self.table=table
        self.index+=1
        self.segment=f'{table}-{self.session}-local-{self.index:03d}'
        self.segment_reason=reason
        self.peak=0
        self.road_count=None
        self.low_hits=0
        self.low_since=None
        self.clear_growth=0
        self.clear_growth_last=None
        self.clear_growth_tick=None
        self.big_drop_noted=False
        self.road_tick=-1e12
        self.reset_pause_until=tick+(3.0 if reason.startswith('VISUAL_ROAD_CLEAR_') else 1.5)
        self.changed_epoch+=1
        self.invalidation_needed=True
        print(f'[NEW LOCAL SEGMENT] table={table} shoe={self.segment} cause={reason}')
        log_event(self.output,'local_segment_opened',table=table,local_shoe_segment=self.segment,
                  boundary_reason=reason,prior_road_peak=prior_peak,
                  note='VISUAL/local identifier only; NOT DG official shoe ID')

    def visible_table(self,tick):
        """Return only a recently re-confirmed table, never a cached old room."""
        if self.table_unconfirmed or not self.table or tick-self.table_tick>12.0:
            return None
        return self.table

    def _make_table_uncertain(self,pending=None):
        if self.table and not self.table_unconfirmed:
            self.table_unconfirmed=True
            self.invalidation_needed=True
            print(f'[TABLE CHECK] Previous table {self.table} is NOT confirmed on current screen; pause candidate saves.')
            log_event(self.output,'table_confirmation_lost',last_confirmed_table=self.table,
                      pending_table=pending,note='Do not save candidates under stale table')
        self.pending_table=pending

    def apply_header(self,read,tick):
        new_table=normalize_current_table(read.get('table'))
        dealer=read.get('dealer')
        if new_table:
            self.table_ocr_failures=0
            self.table_history.append((new_table,tick))
            recent=[(v,when) for v,when in self.table_history if tick-when<=14.0]
            need=2 if self.table is None else 3
            stable=(len(recent)>=need and all(v==new_table for v,when in recent[-need:]))
            if self.table is None:
                if stable:
                    self.new_segment(new_table,'OBSERVER_STARTED_MID_SHOE_UNKNOWN',tick)
                    self.table_tick=tick
                    self.table_unconfirmed=False
                    self.pending_table=None
            elif new_table != self.table:
                # New table needs THREE consecutive positive visual readings.
                # Immediately pause the previous room to prevent cross-room data.
                self._make_table_uncertain(new_table)
                if stable:
                    old=self.table
                    self.new_segment(new_table,'VISIBLE_TABLE_SWITCH',tick)
                    self.table_tick=tick
                    self.table_unconfirmed=False
                    self.pending_table=None
                    self.table_history.clear()
                    self.table_history.append((new_table,tick))
                    log_event(self.output,'table_confirmed',table=new_table,previous_table=old)
            else:
                # A previously questionable header must re-confirm the SAME
                # table twice, rather than immediately reusing the old label.
                two_same=len(recent)>=2 and all(v==self.table for v,when in recent[-2:])
                if not self.table_unconfirmed or two_same:
                    self.table_tick=tick
                    self.table_unconfirmed=False
                    self.pending_table=None
        else:
            self.table_ocr_failures+=1
            # Even without a dealer change, repeated readable-header / unreadable
            # table observations must not inherit the previous room label.
            if self.table and self.table_ocr_failures>=2:
                self._make_table_uncertain()

        if dealer and re.fullmatch(r'[A-Za-z]{2,18}',dealer):
            self.dealer_history.append((dealer,tick))
            recent=[v for v,when in self.dealer_history if tick-when<=18.]
            confirmed=None
            for name in reversed(recent):
                if sum(v.lower()==name.lower() for v in recent)>=2:
                    confirmed=name
                    break
            if confirmed:
                # A dealer rotation can happen without a table switch, but
                # when the table ID itself is unreadable we must not assume
                # either scenario: PAUSE until the current room is confirmed.
                if self.dealer and confirmed.lower()!=self.dealer.lower() and not new_table:
                    self._make_table_uncertain()
                self.dealer=confirmed
                self.dealer_tick=tick


    def apply_timer_visual(self,tick):
        self.timer_visual_tick=tick

    def apply_timer(self,value,tick):
        """Reject impossible jumps, stale 1s readings, and one-off rollovers.

        These seconds are game-screen observations, NOT official clocks.
        """
        if value is None:
            if tick-self.timer_tick>5.:
                self.timer=None
            return
        val=int(value)
        if self.timer_frozen_value is not None:
            if val==self.timer_frozen_value:
                return
            self.timer_frozen_value=None
        if not 0<=val<=60:
            return
        self.timer_raw_history.append((val,tick))
        # A single short digit when no history exists cannot reliably prove
        # the countdown; 20 has been misread as 1 on this machine.
        if self.timer is None:
            recent=[v for v,when in self.timer_raw_history if tick-when<=3.]
            if val < 10 and recent.count(val)<2:
                return
            self._set_timer(val,tick)
            return
        elapsed=max(0.0,tick-self.timer_tick)
        if val==self.timer:
            # Display stuck at 1 for >5s should become unknown rather than
            # endlessly showing a valid-looking "1".
            if tick-self.timer_last_change_tick>5.5:
                self.timer_frozen_value=val
                self.timer=None
                self.timer_raw_history.clear()
                return
            self.timer_tick=tick
            return
        # Valid countdown can drop by about elapsed seconds. A fall from 20 to
        # 1 in one second is not believable.
        if val<self.timer:
            if self.timer-val > max(3,round(elapsed*1.8)+2):
                return
            self._set_timer(val,tick)
            return
        # Increasing digits require two matching readings: another betting
        # window may have started, or it might be an OCR hallucination.
        recent=[v for v,when in self.timer_raw_history if tick-when<=4.]
        if recent.count(val)>=2 and (tick-self.timer_last_change_tick>=1.0):
            self._set_timer(val,tick)

    def _set_timer(self,val,tick):
        previous=self.timer
        self.timer=val
        self.timer_tick=tick
        self.timer_visual_tick=tick
        self.timer_at=clock()
        if previous!=val:
            self.timer_last_change_tick=tick
        if self.timer!=self.timer_last_logged:
            self.timer_last_logged=self.timer
            log_event(self.output,'visible_countdown',table=self.table,seconds=self.timer,
                      official_countdown=False)

    def _clear_pending_reset(self):
        self.low_hits=0
        self.low_since=None
        self.clear_growth=0
        self.clear_growth_last=None
        self.clear_growth_tick=None

    def apply_road(self,measurement,tick):
        """Conservative road reset: 47 -> 12 is NEVER enough for a new shoe.

        Need a nearly-empty, clear grid (0..2), multiple independent frames,
        AND gradual growth of new beads. Any rebound to old history cancels it.
        """
        if not measurement:
            self.road_count=None
            self.road_tick=-1e12
            self._clear_pending_reset()
            return
        markers=int(measurement['markers'])
        neutral=float(measurement['neutral'])
        self.road_neutral=neutral
        self.road_count=markers
        self.road_tick=tick
        if not self.table or tick-self.table_tick>12.0:
            self._clear_pending_reset()
            return
        self.peak=max(self.peak,markers)
        if self.peak>=20 and markers<=int(self.peak*.40) and markers>2 and not self.big_drop_noted:
            self.big_drop_noted=True
            print(f'[ROAD ANOMALY] {self.peak} -> {markers}: NOT a shoe reset; keep current segment.')
            log_event(self.output,'road_count_drop_untrusted',table=self.table,
                      peak=self.peak,visible_markers=markers,
                      note='Drop is NOT proof of shuffle; current local shoe remains unchanged.')
        fresh_timer=tick-self.timer_visual_tick <=6.
        clear=(self.peak>=max(20,self.min_prior_markers) and
               markers<=2 and neutral>=0.68 and fresh_timer)
        if self.low_since is None:
            if clear:
                self.low_since=tick
                self.low_hits=1
                self.clear_growth_last=markers
                print(f'[POSSIBLE CLEAR] road {self.peak} -> {markers}; waiting for NEW beads, not switching yet.')
                log_event(self.output,'road_near_empty_suspected',peak=self.peak,markers=markers)
            return
        # Pending clear must remain on the same reliably detected table.
        if markers>max(10,int(self.peak*.60)) or tick-self.low_since>180:
            log_event(self.output,'road_clear_cancelled',table=self.table,markers=markers,
                      note='Old grid returned or insufficient followup evidence.')
            self._clear_pending_reset()
            return
        if clear and self.clear_growth==0:
            if self.clear_growth_last is None or self.clear_growth_last!=markers:
                self.low_hits+=1
            elif tick-self.low_since>=0.8*self.low_hits:
                self.low_hits+=1
            self.clear_growth_last=markers
            return
        if self.low_hits>=3 and tick-self.low_since>=2.0 and 3<=markers<=8 and neutral>=0.68:
            if self.clear_growth_last is None or markers>self.clear_growth_last:
                self.clear_growth+=1
                self.clear_growth_last=markers
                self.clear_growth_tick=tick
            if (self.clear_growth>=3 and tick-self.low_since>=5.0 and
                tick-self.table_tick<=6. and fresh_timer):
                previous=self.peak
                self.new_segment(self.table,'VISUAL_ROAD_CLEAR_AND_REGROWTH_INFERRED',tick,prior_peak=previous)
                self.peak=markers
                self.road_count=markers
                self.road_tick=tick
                print('[INFO] Strong near-empty + gradual regrowth; new UNVERIFIED local segment. Verify actual shuffle.')
            return
        # 3..8 after only 1 or 2 low images, a rising overlay, or inconsistent
        # counts are NOT conclusive; keep monitoring in the same segment.
        if self.low_hits<3 and markers>2:
            self._clear_pending_reset()

    def validate_candidate(self,tick):
        if self.table_unconfirmed:
            return 'TABLE_ID_UNCONFIRMED'
        if not self.visible_table(tick):
            return 'TABLE_NOT_FRESH'
        if not self.segment:
            return 'NO_LOCAL_SHOE_SEGMENT'
        if self.road_count is None or tick-self.road_tick>4.0:
            return 'ROAD_NOT_VISIBLE'
        if self.low_since is not None:
            return 'SHOE_CLEAR_UNCONFIRMED_PAUSE'
        if tick < self.reset_pause_until:
            return 'SEGMENT_TRANSITION_WAIT'
        return None

    def maybe_invalidate(self,score_rec):
        if self.invalidation_needed:
            self.invalidation_needed=False
            score_rec.invalidate_episode()

    def write_status(self,tick,force=False):
        if tick-self.last_status_tick < 3.0 and not force:
            return
        self.status=self.validate_candidate(tick) or 'OBSERVING_UNVERIFIED'
        if self.last_status!=self.status:
            print('[status]',self.status,'table=',self.visible_table(tick) or '?', 'last_confirmed=',self.table or '?','local_shoe=',self.segment or '?')
            log_event(self.output,'observer_status',status=self.status,table=self.table,
                      local_shoe_segment=self.segment)
            self.last_status=self.status
        dg.write_json_safe(self.output/'DG_V6_STATUS.json',{
            'updated_at':clock(), 'observer_status':self.status,
            'current_table_ocr':self.visible_table(tick),
            'last_confirmed_table':self.table,
            'pending_table_ocr':self.pending_table,
            'table_ocr_consecutive_failures':self.table_ocr_failures,
            'current_dealer_ocr':self.dealer if tick-self.dealer_tick<=15 else None,
            'local_shoe_segment':self.segment,
            'shoe_boundary_reason':self.segment_reason,
            'shoe_id_official':None,
            'shoe_reset_suspected':self.low_since is not None,
            'bead_markers_visible':self.road_count,
            'untrusted_drop_seen':self.big_drop_noted,
            'near_empty_frames':self.low_hits,
            'new_bead_growth_confirmations':self.clear_growth,
            'max_bead_markers_since_segment':self.peak,
            'displayed_countdown':self.timer if tick-self.timer_tick<=5 else None,
            'warning':'Never infer shuffle from a road count drop alone. Shoe IDs/rounds are NOT official; keep all crops visible.'
        })
        self.last_status_tick=tick


class V6Recorder(score.OverlayRecorder):
    def __init__(self,ctx,output,*,ocr_every=0.40):
        super().__init__(output,ocr_every=ocr_every)
        self.ctx=ctx
        self.begin_segment=None
        self.begin_table=None
        self.last_written=-1e12
        self.reviewed=0

    def invalidate_episode(self):
        # Drop an unfinished overlay whenever the confirmed table or inferred
        # shoe changes. In-flight OCR results are ignored by episode_id.
        self.episode_id+=1
        self.episode=None
        self.missing=0
        self.readings=[]
        self.last_announced=None
        self.last_capture=None
        self.begin_segment=None
        self.begin_table=None
        self.last_written=-1e12  # duplicate guard is scoped to one local shoe segment

    def feed(self,frame,seen_at,*,tick=None):
        if self.episode is None and score.banner_visible(score.normalized_strip(frame)):
            self.begin_segment=self.ctx.segment
            self.begin_table=self.ctx.table
        return super().feed(frame,seen_at,tick=tick)

    def _stable_vote(self):
        primary=super()._stable_vote()
        if primary:
            return primary
        # Controlled fallback for short overlays: two independent, high-quality
        # *final* readings must agree >=0.45s apart. Never save in-flight scores.
        if self.last_visible_time is None:
            return None
        recent=[r for r in self.readings if
                self.last_visible_time-2.2<=r['sec']<=self.last_visible_time+0.001]
        if len(recent)<2:
            return None
        a,b=recent[-2:]
        if (a['pair']==b['pair'] and 0.45<=b['sec']-a['sec']<=2.0 and
            self.last_visible_time-b['sec']<=1.2 and
            a.get('confidence',0)>=55 and b.get('confidence',0)>=55):
            return {'pair':b['pair'],'agreement':2,'reading':b,
                    'quality':'TWO_STABLE_READINGS_NEEDS_REVIEW'}
        return None

    def _review(self,reason):
        self.skipped+=1
        self.reviewed+=1
        fields={'reason':reason,'table':self.ctx.visible_table(time.monotonic()),
                'previous_confirmed_table':self.ctx.table,
                'local_shoe_segment':self.ctx.segment,
                'last_score_seen_at':self.last_visible_stamp,
                'ocr_readings_in_episode':len(self.readings)}
        if self.last_capture is not None:
            target=self.output/'review'/f'review_{self.ctx.session}_{self.reviewed:05d}.png'
            try:
                actual=score.write_png_unicode(target,self.last_capture)
                fields['score_crop_image']=str(actual)
            except Exception as exc:
                fields['evidence_error']=str(exc)[:140]
        log_event(self.ctx.output,'candidate_needs_review',**fields)
        print(f'[REVIEW] {reason}; table={self.ctx.visible_table(time.monotonic()) or "?"}, last_confirmed={self.ctx.table or "?"}, '
              f'last_score={self.last_visible_stamp}, total_reviews={self.reviewed}')

    def finish(self):
        now=time.monotonic()
        result=self._stable_vote()
        if result is None:
            self._review('UNSTABLE_FINAL_SCORE')
            return
        bad=self.ctx.validate_candidate(now)
        if bad is None and (self.begin_segment!=self.ctx.segment or self.begin_table!=self.ctx.table):
            bad='TABLE_OR_SHOE_CHANGED_DURING_OVERLAY'
        if bad is None and now-self.last_written<12:
            bad='POSSIBLE_DUPLICATE_TOO_CLOSE'
        if bad:
            self._review(bad)
            return
        p,b=result['pair']
        self.number+=1
        sid=self.ctx.segment
        cid=f'dg-{self.ctx.session}-{self.number:05d}'
        actual=score.write_png_unicode(self.output/'evidence'/f'{cid}.png',result['reading']['strip'])
        save_at=clock()
        lag=(max(0,round((now-self.last_visible_time)*1000))
             if self.last_visible_time is not None and not self.offline else None)
        row={
            'candidate_id':cid,'table':self.ctx.table,'table_detected':self.ctx.table,
            'dealer':self.ctx.dealer if now-self.ctx.dealer_tick<=15 else None,'local_shoe_segment':sid,
            'shoe_boundary_reason':self.ctx.segment_reason,
            'shoe_id_official':None,'player_points':p,'banker_points':b,
            'winner':'PLAYER' if p>b else 'BANKER' if b>p else 'TIE',
            'score_seen_at':self.last_visible_stamp,'saved_at':save_at,
            'capture_delay_ms':lag,
            'timer_last_seen':self.ctx.timer if now-self.ctx.timer_tick<5 else None,
            'verified':False,'official_round_id':None,'official_settled_at':None,
            'agreement_samples':result['agreement'],
            'evidence_image':str(actual.relative_to(self.output)) if actual.is_relative_to(self.output) else str(actual),
            'warning':('UNVERIFIED_WEAK_2_READINGS_CHECK_MANUALLY'
                       if result['agreement']==2 else 'UNVERIFIED_SCREEN_CANDIDATE_SHOE_BOUNDARY_NOT_OFFICIAL')
        }
        per_shoe=self.output/'shoes'/f'{sid}.csv'
        for target in (per_shoe,self.output/'DG_V6_ALL_CANDIDATES.csv'):
            target.parent.mkdir(parents=True,exist_ok=True)
            fresh=not target.exists() or target.stat().st_size==0
            with target.open('a',newline='',encoding='utf-8-sig') as f:
                writer=csv.DictWriter(f,fieldnames=CSV_FIELDS,extrasaction='ignore')
                if fresh:writer.writeheader()
                writer.writerow(row)
        dg.append_jsonl(self.output/'DG_V6_ALL_CANDIDATES.jsonl',row)
        self.hits+=1
        self.last_written=now
        log_event(self.ctx.output,'candidate_saved',candidate_id=cid,table=self.ctx.table,
                  local_shoe_segment=sid,player_points=p,banker_points=b,
                  vote_strength=result['agreement'])
        print(f'[CANDIDATE UNVERIFIED] table={self.ctx.table}, local_shoe={sid}, P={p}, B={b}, '
              f'agreement={result["agreement"]}, lag={lag if lag is not None else "offline"}ms, total_saved={self.hits}')



def calibrate_road():
    import mss
    score_reg=score_region()
    print('ROAD CALIBRATION: select ONLY the left bead-road six-row history grid.')
    print('Do NOT select the big-road or the whole bottom panel; no login tokens included.')
    print('Sample image: samples/ROAD_GRID_EXAMPLE.png')
    input('Open DG with the bead-road grid visible, press ENTER to capture screen...')
    hwnd=dg.minimize_console()
    try:
        time.sleep(1.3)
        with mss.mss() as sct:
            mon=dg.find_monitor_for_score(sct.monitors,score_reg)
            frame=dg.capture(sct,mon)
    finally:
        dg.restore_console(hwnd)
    h,w=frame.shape[:2]
    ratio=min(1.0,1550/w,880/h)
    preview=cv2.resize(frame,(round(w*ratio),round(h*ratio)),interpolation=cv2.INTER_LINEAR)
    try:
        roi=cv2.selectROI('SELECT 6-ROW BEAD ROAD ONLY - PRESS ENTER',preview,
                          showCrosshair=True,fromCenter=False)
    finally:
        cv2.destroyAllWindows()
    x,y,rw,rh=roi
    if rw<75 or rh<55:
        raise RuntimeError('No bead-road grid selected; existing config was not overwritten.')
    rect={'left':mon['left']+round(x/ratio),'top':mon['top']+round(y/ratio),
          'width':round(rw/ratio),'height':round(rh/ratio)}
    with mss.mss() as sct:
        road=dg.capture(sct,rect)
    test=marker_stats(road)
    if test is None:
        raise RuntimeError('Selected crop does not look like a bright 6-row grid. Recalibrate; old config unchanged.')
    dg.write_json_safe(ROAD_FILE,{'version':6,'created_at':clock(), 'road':rect,
                    'kind':'BEAD_ROAD_6_ROWS','warning':'OCR/visual shoe inference, not official shoe ID'})
    out=OUTPUT/'previews'/f'bead_road_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png'
    actual=score.write_png_unicode(out,road)
    print('[OK] road region:',rect,'marker_estimate:',test['markers'],'preview:',actual)
    print('Next: 02_START_V6.cmd. Keep layout/zoom unchanged.')



def calibrate_all():
    """One screen capture; select dealer/table, green timer, and 6-row bead-road."""
    import mss
    reg=score_region()
    print('SINGLE SCREEN CALIBRATION: current DG Chrome must be visible at original size.')
    print('Select in order: 1) dealer/table header  2) entire green countdown circle  3) six-row bead road grid.')
    print('Reference crops: samples/HEADER_Sandy_RB02.png, TIMER_22.png, ROAD_GRID_EXAMPLE.png')
    input('When ALL THREE areas are visible on screen, press ENTER to take ONE still screenshot...')
    hwnd=dg.minimize_console()
    try:
        time.sleep(1.3)
        with mss.mss() as sct:
            mon=dg.find_monitor_for_score(sct.monitors,reg)
            frame=dg.capture(sct,mon)
    finally:
        dg.restore_console(hwnd)
    height,width=frame.shape[:2]
    ratio=min(1.0,1550/width,880/height)
    displayed=cv2.resize(frame,(round(width*ratio),round(height*ratio)), interpolation=cv2.INTER_LINEAR)
    names=[('header','SELECT DEALER AND TABLE HEADER - ENTER'),
           ('timer','SELECT GREEN COUNTDOWN CIRCLE - ENTER'),
           ('road','SELECT SIX ROW BEAD ROAD GRID - ENTER')]
    found={}
    try:
        for name,title in names:
            raw=cv2.selectROI(title,displayed,showCrosshair=True,fromCenter=False)
            cv2.destroyWindow(title)
            x,y,w,h=raw
            if w<45 or h<24 or (name=='road' and (w<75 or h<60)):
                raise RuntimeError('ROI selection canceled / too small: '+name+'. Previous calibration unchanged.')
            found[name]={
                'left':mon['left']+round(x/ratio),
                'top':mon['top']+round(y/ratio),
                'width':round(w/ratio),'height':round(h/ratio)
            }
    finally:
        cv2.destroyAllWindows()
    # Analyze each region from the original single still. Do NOT recapture the
    # desktop after selection (it could have changed to ChatGPT or another tab).
    def from_original(rect):
        x=rect['left']-mon['left']; y=rect['top']-mon['top']
        w=rect['width'];h=rect['height']
        return frame[y:y+h,x:x+w]
    header_img=from_original(found['header'])
    timer_img=from_original(found['timer'])
    road_img=from_original(found['road'])
    road_metrics=marker_stats(road_img)
    header_decoded=parse_live_header(header_img)
    timer_decoded=dg.parse_timer(timer_img)
    print('[CHECK] header:',header_decoded, 'timer:',timer_decoded,'road:',road_metrics)
    if road_metrics is None:
        raise RuntimeError('Road crop is not a bright bead grid. No calibration written.')
    if not header_decoded.get('table'):
        raise RuntimeError('Current table ID not recognized in header selection. No calibration written.')
    # Timer digits might be blank on the captured frame (timer still selected).
    dg.write_json_safe(dg.V4_CONFIG,{'version':4,'created_at':clock(),
                       'header':found['header'],'timer':found['timer'],
                       'note':'Read-only live current-table header and timer region.'})
    dg.write_json_safe(ROAD_FILE,{'version':6,'created_at':clock(),
                       'road':found['road'],'kind':'BEAD_ROAD_6_ROWS',
                       'note':'Visual road-reset heuristic, not official shoe ID.'})
    preview_dir=OUTPUT/'previews'
    preview_dir.mkdir(parents=True,exist_ok=True)
    hp=score.write_png_unicode(preview_dir/'header_redacted.png',dg.sanitized_header_preview(header_img))
    tp=score.write_png_unicode(preview_dir/'timer.png',timer_img)
    rp=score.write_png_unicode(preview_dir/'road.png',road_img)
    print('[OK] All regions saved. Header preview redacts right-side ID:',hp)
    print('[OK] Timer preview:',tp)
    print('[OK] Six-row road preview:',rp)
    print('If visual crops are correct, start 02_START_V6.cmd. DG browser position must remain unchanged.')

def manual_boundary():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    request=OUTPUT/'MANUAL_NEW_SHOE_REQUEST.txt'
    request.write_text(clock(),encoding='utf-8')
    print('[OK] Operator marked an *observed* shuffle. Live V6 will open a new local segment.')


def run_monitor(args):
    import mss
    with dg.single_instance_lock(OUTPUT):
        regs=v4_regions()
        OUTPUT.mkdir(parents=True,exist_ok=True)
        ctx=ShoeTracker(OUTPUT)
        rec=V6Recorder(ctx,OUTPUT,ocr_every=args.score_ocr_every)
        pending_header=None;pending_timer=None
        next_header=0;next_timer=0;next_road=0;next_report=0
        last_header_request=-1e12
        worker=ThreadPoolExecutor(max_workers=2,thread_name_prefix='dg-v5-metadata')
        print('DG V6.4 TABLE-SAFE + SHOE OBSERVER (VISIBLE SCREEN / READ ONLY)')
        print('Input regions:',regs)
        print('No automatic login, fake bets, 404 bypass, or official settlement claims.')
        print('All results UNVERIFIED. Near-empty road + fresh growth required for inferred reset.')
        try:
            with mss.mss() as sct:
                while True:
                    t=time.monotonic()
                    if pending_header and pending_header[0].done():
                        task,when=pending_header
                        try:ctx.apply_header(task.result(),when)
                        except Exception as e:print('[WARN] Header OCR:',type(e).__name__,str(e)[:140])
                        pending_header=None
                    if pending_timer and pending_timer[0].done():
                        task,when=pending_timer
                        try:ctx.apply_timer(task.result(),when)
                        except Exception as e:print('[WARN] Timer OCR:',type(e).__name__,str(e)[:140])
                        pending_timer=None
                    if pending_header is None and t>=next_header:
                        pending_header=(worker.submit(parse_live_header,dg.capture(sct,regs['header'])),t)
                        next_header=t+args.header_interval
                    if pending_timer is None and t>=next_timer:
                        timer_frame=dg.capture(sct,regs['timer'])
                        visual=cv2.resize(timer_frame,(dg.TIMER_W,dg.TIMER_H),interpolation=cv2.INTER_CUBIC)
                        if dg.timer_visual_is_present(visual):
                            ctx.apply_timer_visual(t)
                        pending_timer=(worker.submit(dg.parse_timer,timer_frame),t)
                        next_timer=t+args.timer_interval
                    if t>=next_road:
                        ctx.apply_road(marker_stats(dg.capture(sct,regs['road'])),t)
                        next_road=t+args.road_interval
                    # Manual fallback only marks that the operator saw an actual
                    # shuffle. This never claims DG official shoe IDs.
                    trigger=OUTPUT/'MANUAL_NEW_SHOE_REQUEST.txt'
                    if trigger.is_file() and ctx.table and t-ctx.table_tick<=5:
                        try:trigger.unlink()
                        except OSError:pass
                        ctx.new_segment(ctx.table,'USER_CONFIRMED_VISUAL_SHUFFLE',t,prior_peak=ctx.peak)
                        ctx.peak=ctx.road_count or 0
                    ctx.maybe_invalidate(rec)
                    strip=dg.capture(sct,regs['score'])
                    rec.feed(strip,clock(),tick=t)
                    ctx.write_status(t)
                    if t>=next_report:
                        print(f'[running] table={ctx.visible_table(t) or "?"} last_confirmed={ctx.table or "?"} dealer={ctx.dealer or "?"} '
                              f'local_shoe={ctx.segment or "?"} road={ctx.road_count} peak={ctx.peak} '
                              f'timer={ctx.timer} status={ctx.status} saved={rec.hits} review={rec.reviewed}')
                        next_report=t+12
                    time.sleep(max(0,args.interval-(time.monotonic()-t)))
        except KeyboardInterrupt:
            print('\n[STOP] V6 candidate observer stopped safely. Saved:',rec.hits)
        finally:
            rec.close()
            worker.shutdown(wait=False,cancel_futures=True)
            ctx.write_status(time.monotonic(),force=True)


def check_current_screen():
    """One static capture with existing four calibration regions; no reselect."""
    import mss
    regs=v4_regions()
    print('[V6] Checking currently VISIBLE DG screen. Do not cover Chrome or change its size.')
    console=dg.minimize_console()
    try:
        time.sleep(1.3)
        with mss.mss() as sct:
            imgs={name:dg.capture(sct,rect) for name,rect in regs.items()}
    finally:
        dg.restore_console(console)
    head=parse_live_header(imgs['header'])
    timer=dg.parse_timer(imgs['timer'])
    road=marker_stats(imgs['road'])
    seen=score.banner_visible(score.normalized_strip(imgs['score']))
    print(f'[LIVE OCR] table={head.get("table") or "?"}, dealer={head.get("dealer") or "?"}, '
          f'timer={timer}, road_markers={road["markers"] if road else "?"}, score_banner={seen}')
    previews=OUTPUT/'previews'
    previews.mkdir(parents=True,exist_ok=True)
    for name,im in imgs.items():
        if name=='header':im=dg.sanitized_header_preview(im)
        actual=score.write_png_unicode(previews/f'v6_{name}.png',im)
        print(f'[PREVIEW] {name}: {actual}')
    print('Preview images contain selected crops ONLY. Header account ID is obscured.')
    if not head.get('table'):
        print('[WARN] Table not reliably readable. No old table will be substituted.')
        # Print only the account-free left header table-text OCR attempts.
        # Helps diagnose RB05/other rooms without publishing any signed URL.
        print('[TABLE OCR DIAGNOSTIC]', dg.table_ocr_diagnostics(imgs['header']))
        print('[ACTION] Check v6_header.png against the new room, then adjust OCR only if needed.')
    if timer is None:
        print('[WARN] Countdown unreadable or absent; it will be UNKNOWN, not guessed.')
    if road is None:
        print('[WARN] Bead road missing. A score cannot be assigned to a reliable shoe segment.')
    print('[NOTE] This is visual OCR only; all outcomes/shoe IDs remain unverified.')


def self_test():
    """Regression: user's real screenshots + simulated false/reset transitions."""
    from tempfile import TemporaryDirectory
    header1=parse_live_header(decode_png(SAMPLES/'HEADER_Sandy_RB02.png'))
    header2=parse_live_header(decode_png(SAMPLES/'HEADER_HAPPY_RB03.png'))
    seconds22=dg.parse_timer(decode_png(SAMPLES/'TIMER_22.png'))
    seconds20=dg.parse_timer(decode_png(SAMPLES/'TIMER_20.png'))
    road=marker_stats(decode_png(SAMPLES/'ROAD_GRID_EXAMPLE.png'))
    print('[samples]',header1,header2,'timer22=',seconds22,'timer20=',seconds20,'road=',road)
    assert header1=={'dealer':'Sandy','table':'RB02'}, header1
    assert header2=={'dealer':'HAPPY','table':'RB03'}, header2
    assert seconds22==22 and seconds20==20,(seconds22,seconds20)
    assert normalize_current_table('RBOS') is None,'RBOS must not become RB05'
    assert road is not None and road['markers']>=2
    with TemporaryDirectory() as temp:
        t=ShoeTracker(temp)
        for tick in (1.,3.):t.apply_header({'table':'RB04','dealer':'Sandi'},tick)
        assert t.table=='RB04' and t.index==1
        old=t.segment
        # Peak 47 followed by repeated 12: THIS USER CONFIRMED NO SHUFFLE.
        for tick in (5.,7.,9.,11.,13.):
            t.apply_header({'table':'RB04','dealer':'Sandi'},tick)
            t.apply_timer_visual(tick)
            t.apply_road({'markers':47,'neutral':0.60},tick)
        for tick in (15.,17.,19.,21.,23.,25.):
            t.apply_header({'table':'RB04','dealer':'Sandi'},tick)
            t.apply_timer_visual(tick)
            t.apply_road({'markers':12,'neutral':0.59},tick)
        assert t.index==1 and t.segment==old and t.low_since is None, '47 -> 12 falsely switched shoe'
        assert t.validate_candidate(25.5) is None
        # A white road overlay that briefly looks empty then returns must NOT split.
        for tick in (27.,29.):
            t.apply_header({'table':'RB04','dealer':'Sandi'},tick)
            t.apply_timer_visual(tick)
            t.apply_road({'markers':0,'neutral':0.91},tick)
        t.apply_header({'table':'RB04','dealer':'Sandi'},31.)
        t.apply_road({'markers':47,'neutral':0.60},31.)
        assert t.index==1 and t.low_since is None,'Rebound from overlay must cancel reset'
        # Strong actual clear + multiple frames + new beads rise creates an inferred
        # local segment. It is still NOT a confirmed official DG shoe.
        for tick,m in ((33.,1),(35.,1),(37.,1),(39.,3),(41.,4),(43.,5)):
            t.apply_header({'table':'RB04','dealer':'Sandi'},tick)
            t.apply_timer_visual(tick)
            t.apply_road({'markers':m,'neutral':0.94},tick)
        assert t.index==2 and t.segment!=old,(t.index,t.segment,t.low_hits,t.clear_growth)
        assert t.segment_reason=='VISUAL_ROAD_CLEAR_AND_REGROWTH_INFERRED'
        t.apply_header({'table':'RB03','dealer':'HAPPY'},45.)
        t.apply_header({'table':'RB03','dealer':'HAPPY'},47.)
        assert t.table=='RB04','Table switches must require >=3 OCR reads'
        t.apply_header({'table':'RB03','dealer':'HAPPY'},49.)
        assert t.table=='RB03' and t.index==3
        t.apply_road(None,51.)
        assert t.index==3
    with TemporaryDirectory() as temp:
        t=ShoeTracker(temp)
        t.apply_timer(20,10.)
        t.apply_timer(1,11.)
        assert t.timer==20,'Impossible 20 -> 1 jump accepted'
        t.apply_timer(19,12.)
        assert t.timer==19,'Normal countdown drop not accepted'
        # Test short-overlay two-good-reading fallback, not an official result.
        rec=V6Recorder(t,temp)
        try:
            now=time.monotonic()
            rec.last_visible_time=now
            rec.readings=[{'pair':(8,4),'confidence':70,'sec':now-.8,'strip':np.zeros((58,420,3),np.uint8)},
                          {'pair':(8,4),'confidence':72,'sec':now-.15,'strip':np.zeros((58,420,3),np.uint8)}]
            vote=rec._stable_vote()
            assert vote and vote['pair']==(8,4) and vote['agreement']==2,vote
        finally:
            rec.close()
    # A new dealer with unreadable table must NOT keep attributing to RB03.
    with TemporaryDirectory() as temp:
        s=ShoeTracker(temp)
        s.apply_header({'table':'RB03','dealer':'BEAW'},1.)
        s.apply_header({'table':'RB03','dealer':'BEAW'},3.)
        s.apply_header({'table':'RB03','dealer':'BEAW'},4.)
        assert s.visible_table(4.)=='RB03'
        s.apply_header({'table':None,'dealer':'HAPPY'},5.)
        s.apply_header({'table':None,'dealer':'HAPPY'},7.)
        assert s.visible_table(7.) is None and s.table=='RB03'
        assert s.validate_candidate(7.)=='TABLE_ID_UNCONFIRMED'
        for tick in (9.,11.,13.):
            s.apply_header({'table':'RB05','dealer':'HAPPY'},tick)
        assert s.visible_table(13.)=='RB05' and s.index==2 and s.segment_reason=='VISIBLE_TABLE_SWITCH'
        s.road_count=24; s.road_tick=15.
        assert s.validate_candidate(15.) is None
        # One bad OCR must not silently switch the verified current table.
        s.apply_header({'table':'RB03','dealer':'HAPPY'},16.)
        assert s.visible_table(16.) is None and s.table=='RB05'
        s.apply_header({'table':'RB05','dealer':'HAPPY'},18.)
        s.apply_header({'table':'RB05','dealer':'HAPPY'},20.)
        assert s.visible_table(20.)=='RB05' and s.index==2
    print('[PASS] Real switch RB03 -> RB05 requires three consecutive reads; stale RB03 paused.')
    print('[PASS] Sandy/RB02, HAPPY/RB03 (not RB05), countdown 20 and 22,')
    print('       confirmed NO RESET on 47->12 or transient blank,')
    print('       inferred segment only on sustained clear + regrowth, stable table switch,')
    print('       timer anti-jump and two-high-confidence-score fallback.')
    print('[NOTE] Still VISUAL/UNVERIFIED. Live DG rounds and official shoe IDs not proven.')


def main():
    dg.set_display_dpi()
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--self-test',action='store_true')
    ap.add_argument('--check-live',action='store_true')
    ap.add_argument('--calibrate-road',action='store_true')
    ap.add_argument('--calibrate-all',action='store_true')
    ap.add_argument('--monitor',action='store_true')
    ap.add_argument('--mark-new-shoe',action='store_true')
    ap.add_argument('--interval',type=float,default=0.25)
    ap.add_argument('--score-ocr-every',type=float,default=0.40)
    ap.add_argument('--header-interval',type=float,default=2.4)
    ap.add_argument('--timer-interval',type=float,default=1.0)
    ap.add_argument('--road-interval',type=float,default=1.25)
    args=ap.parse_args()
    if args.self_test:self_test()
    elif args.check_live:check_current_screen()
    elif args.calibrate_all:calibrate_all()
    elif args.calibrate_road:calibrate_road()
    elif args.mark_new_shoe:manual_boundary()
    elif args.monitor:
        if args.interval<0.20 or args.road_interval<0.8 or args.header_interval<1.5:
            ap.error('Capture intervals too short for reliable OCR.')
        run_monitor(args)
    else:
        ap.print_help()


if __name__=='__main__':
    main()
