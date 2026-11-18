"""Adapt the LOCAL V6.4 image OCR to operator-provided Playwright viewport frames.

Never verifies vendor settlement, never accesses sessions or game network data,
never clicks or performs bets. All records are unverified visual candidates.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import json
import time

import cv2
import numpy as np

import dg_rb02_v6 as dg
from dg_current_shoe_v6 import ShoeTracker, V6Recorder, marker_stats, parse_live_header

REQUIRED = ("score", "header", "timer", "road")


def normalized_roi(raw):
    if not isinstance(raw, dict):
        raise ValueError("ROI must be a dictionary")
    try:
        x, y, w, h = (float(raw[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("ROI needs x,y,w,h") from exc
    if not (0 <= x <= 1 and 0 <= y <= 1 and .01 <= w <= 1 and .01 <= h <= 1
            and x+w <= 1.000001 and y+h <= 1.000001):
        raise ValueError("ROI outside viewport")
    return {"x":round(x,6), "y":round(y,6), "w":round(w,6), "h":round(h,6)}


def crop_viewport(frame, normalized):
    if frame is None or frame.size == 0:
        raise ValueError("empty frame")
    h, w = frame.shape[:2]
    r=normalized_roi(normalized)
    x1,y1=int(r['x']*w),int(r['y']*h)
    x2,y2=int((r['x']+r['w'])*w),int((r['y']+r['h'])*h)
    return frame[y1:max(y1+1,y2),x1:max(x1+1,x2)].copy()


def decode_jpeg(buf):
    a=np.frombuffer(buf,dtype=np.uint8)
    frame=cv2.imdecode(a,cv2.IMREAD_COLOR)
    if frame is None: raise ValueError("Invalid image from browser")
    return frame


class CloudOCR:
    """Single-page V6 parser with conservative table/shoe guards and CSV output."""
    def __init__(self, rois, output_dir, header_interval=3.0, timer_interval=2.0,
                 road_interval=2.0, score_ocr_every=.8):
        self.rois={k:normalized_roi(rois[k]) for k in REQUIRED}
        self.output=Path(output_dir)
        self.output.mkdir(parents=True,exist_ok=True)
        self.ctx=ShoeTracker(self.output)
        self.rec=V6Recorder(self.ctx,self.output,ocr_every=score_ocr_every)
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='cloud-ocr')
        self.header_pending=None
        self.timer_pending=None
        self.next_header=0
        self.next_timer=0
        self.next_road=0
        self.header_interval=header_interval
        self.timer_interval=timer_interval
        self.road_interval=road_interval
        self.frames=0
        self.closed=False
        self.error=None

    def close(self):
        if self.closed: return
        self.closed=True
        self.rec.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.ctx.write_status(time.monotonic(),force=True)

    def process(self, frame, *, stamp=None, tick=None):
        if self.closed: raise RuntimeError("OCR closed")
        stamp=stamp or datetime.now().astimezone().isoformat(timespec='milliseconds')
        tick=time.monotonic() if tick is None else tick
        self.frames+=1
        # Never reuse observations when a browser page is absent or changed.
        if self.header_pending is not None and self.header_pending.done():
            try:self.ctx.apply_header(self.header_pending.result(),tick)
            except Exception as e:self.error="header_"+type(e).__name__
            self.header_pending=None
        if self.timer_pending is not None and self.timer_pending.done():
            try:self.ctx.apply_timer(self.timer_pending.result(),tick)
            except Exception as e:self.error="timer_"+type(e).__name__
            self.timer_pending=None
        if tick>=self.next_header and self.header_pending is None:
            self.header_pending=self.pool.submit(parse_live_header,crop_viewport(frame,self.rois['header']))
            self.next_header=tick+self.header_interval
        if tick>=self.next_timer and self.timer_pending is None:
            tm=crop_viewport(frame,self.rois['timer'])
            vis=cv2.resize(tm,(dg.TIMER_W,dg.TIMER_H),interpolation=cv2.INTER_CUBIC)
            if dg.timer_visual_is_present(vis):
                self.ctx.apply_timer_visual(tick)
            self.timer_pending=self.pool.submit(dg.parse_timer,tm)
            self.next_timer=tick+self.timer_interval
        if tick>=self.next_road:
            self.ctx.apply_road(marker_stats(crop_viewport(frame,self.rois['road'])),tick)
            self.next_road=tick+self.road_interval
        self.ctx.maybe_invalidate(self.rec)
        self.rec.feed(crop_viewport(frame,self.rois['score']),stamp,tick=tick)
        self.ctx.write_status(tick)
        return self.snapshot(tick)

    def snapshot(self,tick=None):
        tick=time.monotonic() if tick is None else tick
        return {
            'table':self.ctx.visible_table(tick),
            'last_confirmed_table':self.ctx.table,
            'dealer':self.ctx.dealer if tick-self.ctx.dealer_tick<=15 else None,
            'displayed_countdown':self.ctx.timer if tick-self.ctx.timer_tick<=5 else None,
            'road_markers':self.ctx.road_count,
            'local_shoe_segment':self.ctx.segment,
            'state':self.ctx.validate_candidate(tick) or 'OBSERVING_UNVERIFIED',
            'candidate_count':self.rec.hits,
            'review_count':self.rec.reviewed,
            'frames':self.frames,
            'verified':False,
            'error':self.error,
        }


def last_candidates(output_dir,limit=30):
    src=Path(output_dir)/'DG_V6_ALL_CANDIDATES.jsonl'
    if not src.is_file():return []
    items=[]
    try:
        with src.open(encoding='utf-8') as f:
            for line in f:
                try:
                    x=json.loads(line)
                    items.append({k:x.get(k) for k in (
                        'candidate_id','table','dealer','local_shoe_segment',
                        'player_points','banker_points','winner','saved_at','agreement_samples',
                        'capture_delay_ms','verified')})
                    items=items[-limit:]
                except (ValueError,TypeError):continue
    except OSError:return []
    return items
