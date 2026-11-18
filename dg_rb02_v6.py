#!/usr/bin/env python3
# coding: utf-8
# V6.3 LIVE HEADER + TIMER FIX - RB03 numeric-suffix cross-check and green 20 shape guard.
"""DG LOCAL V6 metadata: visible-screen, read-only OCR candidate recorder.

No website login, bypass, betting, keep-alive, cookies, signed URLs, WebSocket,
or access to protected server data. Browser session expiration is reported
for a human to renew using their own authorized procedure.
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
import uuid
from collections import deque
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytesseract

import score_engine_v3 as score

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent if (ROOT.parent / 'RB02_REGION.json').exists() else ROOT
V4_CONFIG = PROJECT / 'DG_RB02_V4_REGIONS.json'
V3_CONFIG = PROJECT / 'RB02_REGION.json'
OUTPUT = PROJECT / 'DG_RB02_V4_OUTPUT'
SAMPLES = ROOT / 'samples'
HEADER_W, HEADER_H = 291, 64
TIMER_W, TIMER_H = 158, 185
CSV_FIELDS = [
    'candidate_id','table','table_detected','table_source','dealer','dealer_source',
    'player_points','banker_points','winner','score_seen_at','saved_at',
    'post_overlay_delay_ms','timer_last_seen','timer_last_seen_at',
    'round_id','settled_at','verified','agreement_samples','evidence_image','source','flags'
]


def clock():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def write_json_safe(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2) + '\n'
    tmp = path.with_suffix(path.suffix + '.tmp')
    try:
        tmp.write_text(data, encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        path.write_text(data, encoding='utf-8')


def append_jsonl(path, item):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(item, ensure_ascii=False) + '\n')


def region_ok(rect):
    return isinstance(rect,dict) and all(isinstance(rect.get(k),int) for k in ('left','top','width','height')) and rect['width'] >= 22 and rect['height'] >= 18


def load_score_region():
    if not V3_CONFIG.is_file():
        raise RuntimeError(f'Original score calibration missing: {V3_CONFIG}. Run original CALIBRATE_RB02.cmd first.')
    saved = json.loads(V3_CONFIG.read_text(encoding='utf-8'))
    region = saved.get('screen_region')
    if not region_ok(region):
        raise RuntimeError('Invalid RB02_REGION.json score region')
    return region


def get_regions():
    if not V4_CONFIG.is_file():
        raise RuntimeError('Dealer/timer calibration missing. Run CALIBRATE_DG_HEADER_TIMER.cmd first.')
    saved = json.loads(V4_CONFIG.read_text(encoding='utf-8'))
    for key in ('header','timer'):
        if not region_ok(saved.get(key)):
            raise RuntimeError(f'Invalid {key} region in DG_RB02_V4_REGIONS.json')
    return {'score':load_score_region(), 'header':saved['header'], 'timer':saved['timer']}


def set_display_dpi():
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ocr_one(img, cfg):
    try:
        return pytesseract.image_to_string(img, config=cfg, timeout=2.0).strip()
    except RuntimeError:
        return ''


def prep_text(img, *, scale=4):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def normalize_table(s):
    """Require explicit numeric digits; S must NOT silently become 5.

    The user's real RB03 sample is regularly misread as RBOS by OCR. Mapping
    S -> 5 would incorrectly turn this into RB05, a different table.
    """
    compact = re.sub(r'[^A-Za-z0-9]', '', str(s)).upper()
    for candidate in re.findall(r'R(?:B|8|E)[A-Z0-9]{2,3}', compact):
        suffix = candidate[2:].replace('O', '0').replace('I', '1').replace('L', '1').replace('Z', '2')
        if suffix.isdigit() and 2 <= len(suffix) <= 3:
            return 'RB' + suffix
    return None


def _inspect_table_region(norm, *, extended=False):
    """Only inspect the table-text ROI; never OCR the account ID at the right.

    OCR evidence is not a source of truth: contradictory readings stay unknown.
    The RBOS ambiguity is NEVER automatically translated into RB05.
    """
    from collections import Counter
    text_attempts=[]
    prefix_count=0
    full_votes=[]
    table_rects=(
        ((70, 115, 34, 61), True, 13),
        ((70, 135, 34, 61), False, 13),
        ((70, 135, 34, 61), True, 13),
        ((65, 130, 34, 61), False, 7),
    )
    for rect, threshold, psm in table_rects:
        x1,x2,y1,y2=rect
        gray=cv2.cvtColor(norm[y1:y2,x1:x2],cv2.COLOR_BGR2GRAY)
        if threshold:
            gray=cv2.threshold(gray,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]
        up=cv2.resize(gray,None,fx=4,fy=4,interpolation=cv2.INTER_CUBIC)
        raw=ocr_one(up,f'--psm {psm} -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
        cleaned=re.sub('[^A-Z0-9]','',raw.upper())[:16]
        text_attempts.append(cleaned)
        if re.search(r'R(?:B|8|E)',cleaned):
            prefix_count+=1
        table=normalize_table(cleaned)
        if table:
            full_votes.append(table)

    # Several numeric-only reads of the two suffix digits.  Unlike mapping
    # 'S' to '5', these reads need to actually report TWO Arabic numerals.
    suffix_attempts=[]
    narrow=cv2.cvtColor(norm[33:63,67:118],cv2.COLOR_BGR2GRAY)
    plans=[(narrow,7),(narrow,13)]
    if extended or not full_votes:
        otsu=cv2.threshold(narrow,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]
        plans.extend([(otsu,7),(otsu,13)])
    for view,psm in plans:
        up=cv2.resize(view,None,fx=4,fy=4,interpolation=cv2.INTER_CUBIC)
        raw=ocr_one(up,f'--psm {psm} -c tessedit_char_whitelist=0123456789')
        cleaned=re.sub('[^0-9]','',raw)[:5]
        suffix_attempts.append(cleaned)

    full_choice=None
    if full_votes:
        proposed,hits=Counter(full_votes).most_common(1)[0]
        if hits>=2 and hits>len(full_votes)/2:
            full_choice=proposed
    digits=[s for s in suffix_attempts if re.fullmatch('[0-9]{2}',s)]
    digit_choice=None
    if digits:
        proposed,hits=Counter(digits).most_common(1)[0]
        if hits>=2 and hits>len(digits)/2 and prefix_count>=1:
            digit_choice='RB'+proposed
    conflict=bool(full_choice and digit_choice and full_choice!=digit_choice)
    table=None if conflict else (digit_choice or full_choice)
    return table,{'table_text_ocr':text_attempts,'table_digits_ocr':suffix_attempts,
                  'prefix_readings':prefix_count,'whole_vote':full_choice,
                  'digit_vote':digit_choice,'conflicting_votes':conflict}


def table_ocr_diagnostics(img):
    """Manual only. Safe to log: crops table text, NEVER session/account ID."""
    if img is None or img.size==0:
        return {'error':'no header image'}
    norm=cv2.resize(img,(HEADER_W,HEADER_H),interpolation=cv2.INTER_CUBIC)
    _,info=_inspect_table_region(norm,extended=True)
    return info


def parse_header(img):
    """Dealer and current RB table OCR, with conservative numerical cross-check."""
    if img is None or img.size==0:
        return {'dealer':None,'table':None}
    norm=cv2.resize(img,(HEADER_W,HEADER_H),interpolation=cv2.INTER_CUBIC)
    name_patch=norm[3:30,57:148]
    name_raw=ocr_one(prep_text(name_patch),
                     '--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
    dealer=re.sub('[^A-Za-z]','',name_raw)
    if not 2<=len(dealer)<=18:
        dealer=None
    table,_=_inspect_table_region(norm)
    return {'dealer':dealer,'table':table}


def timer_visual_is_present(norm):
    # Require the green circular display's central green pixels; this rejects
    # random digits from ChatGPT, 404/error pages, or other desktop windows.
    region=norm[42:143,22:139]
    hsv=cv2.cvtColor(region,cv2.COLOR_BGR2HSV)
    green=((hsv[:,:,0]>=28)&(hsv[:,:,0]<=98)&(hsv[:,:,1]>=38)&(hsv[:,:,2]>=40))
    return float(green.mean())>0.035


def _two_digit_glyphs_visible(img):
    """Conservative geometric cross-check for a two-digit green timer.

    Resolves OCR occasionally dropping a leading digit (e.g. 20 read as 0).
    This is NOT a decoder on its own: a two-digit OCR consensus is still required.
    """
    h, w = img.shape[:2]
    # Include both digits but keep most of the circular ring outside this crop.
    roi = img[round(h * .30):round(h * .65), round(w * .24):round(w * .78)]
    if roi.size == 0:
        return False
    blue, green, red = cv2.split(roi)
    threshold = max(145, float(np.percentile(green, 55)) + 28)
    mask = ((green > threshold) &
            (green.astype(np.int16)-red.astype(np.int16)>35) &
            (green.astype(np.int16)-blue.astype(np.int16)>30)).astype(np.uint8)*255
    # Suppress isolated antialiasing and background glitter.
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    rh, rw = roi.shape[:2]
    plausible = []
    for x, y, bw, bh, area in stats[1:]:
        if (.075*rw <= bw <= .55*rw and .34*rh <= bh <= .98*rh
                and area >= .04*rw*rh):
            plausible.append((int(x), int(x+bw)))
    plausible.sort()
    # Require two separate glyph-sized objects in the center, not ring fragments.
    for i, a in enumerate(plausible):
        for b in plausible[i+1:]:
            if 0 <= b[0]-a[1] <= .2*rw:
                return True
    return False


def _timer_digit_pair(img):
    """Extract two GREEN digit silhouettes; no OCR, language packs or clock guesses.

    Crops only the center of the green countdown ring.  If the glyph count,
    alignment or visibility is doubtful, do not infer a timer from its outline.
    """
    if img is None or img.size == 0:
        return None
    h, w = img.shape[:2]
    if h < 35 or w < 40:
        return None
    roi = img[int(h * .36):int(h * .64), int(w * .34):int(w * .73)]
    if roi.size == 0 or roi.shape[0] < 15 or roi.shape[1] < 20:
        return None
    blue, green, red = cv2.split(roi)
    gv = green.astype(np.int16)
    # Relative brightness is less sensitive to a different Chrome brightness
    # or PNG rendering on another Windows system.
    cutoff = max(95., .68 * float(np.percentile(green, 97)))
    mask = ((gv > cutoff) &
            (gv - red.astype(np.int16) > 28) &
            (gv - blue.astype(np.int16) > 25)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            np.ones((2, 2), dtype=np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    rh, rw = roi.shape[:2]
    found = []
    for idx in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[idx])
        if (area >= max(18, .045 * rh * rw) and
            .16 * rw <= bw <= .59 * rw and
            .42 * rh <= bh <= 1.05 * rh):
            found.append((x, y, bw, bh, area, idx))
    found.sort(key=lambda item: item[0])
    if len(found) != 2:
        return None
    first, last = found
    gap = last[0] - (first[0] + first[2])
    shared_height = min(first[1] + first[3], last[1] + last[3]) - max(first[1], last[1])
    if (not (-2 <= gap <= .13 * rw) or
        shared_height < .40 * min(first[3], last[3])):
        return None
    silhouettes = []
    for x, y, bw, bh, area, idx in found:
        glyph = (labels[y:y + bh, x:x + bw] == idx).astype(np.uint8)
        silhouettes.append(cv2.resize(glyph, (28, 36),
                                       interpolation=cv2.INTER_NEAREST))
    return silhouettes


def _timer_match_template(img):
    """Cross-platform fallback for visually matching the USER-provided 20/22.

    Other numbers are left to multi-pass OCR. A poor match never fabricates a
    timer. References are bundled screenshots stored in samples/TIMER_*.png.
    """
    # Cache on the pure function attribute: no recreation per 1-sec observation.
    if not hasattr(_timer_match_template, '_references'):
        refs = {}
        for sec in (20, 22):
            target = SAMPLES / ('TIMER_' + str(sec) + '.png')
            try:
                data = np.frombuffer(target.read_bytes(), dtype=np.uint8)
                ref = cv2.imdecode(data, cv2.IMREAD_COLOR)
                shapes = _timer_digit_pair(ref)
                if shapes is not None:
                    refs[sec] = shapes
            except (OSError, ValueError, cv2.error):
                pass
        _timer_match_template._references = refs
    now = _timer_digit_pair(img)
    if now is None:
        return None

    def jaccard(a, b):
        union = int(np.count_nonzero(np.logical_or(a, b)))
        return (float(np.count_nonzero(np.logical_and(a, b))) / union
                if union else 0.)

    scores = []
    for sec, reference in _timer_match_template._references.items():
        sims = [jaccard(a, b) for a, b in zip(now, reference)]
        scores.append((min(sims), sum(sims) / 2, sec))
    if not scores:
        return None
    scores.sort(reverse=True)
    best = scores[0]
    other = scores[1] if len(scores) > 1 else (0., 0., -1)
    # The real 136x110 live countdown '20' has differently antialiased 2
    # strokes from the 137x120 bundled sample. Their binary silhouettes
    # still agree strongly (worst glyph ~0.74, mean ~0.826), while '22'
    # scores much lower (~0.54). The old 0.81 min-glyph cutoff discarded
    # this valid 20 entirely. Guard the slightly wider tolerance with BOTH
    # absolute shape quality and a clear margin over the second candidate.
    if best[0] >= .70 and best[1] >= .80 and best[1] - other[1] >= .22:
        return best[2]
    return None


def parse_timer(img):
    """Read countdown with OCR consensus and conservative visual cross-check.

    On some Windows Tesseract installations '20' is returned as '0' even with
    PSM-8 on both polarities.  A strong match to TWO supplied visual digit
    templates corrects that error. Unknown or contradictory screens return None.
    """
    if img is None or img.size == 0:
        return None
    h, w = img.shape[:2]
    if h < 35 or w < 40:
        return None
    norm = cv2.resize(img, (TIMER_W, TIMER_H),
                      interpolation=cv2.INTER_CUBIC)
    if not timer_visual_is_present(norm):
        return None
    patch = img[int(h * .34):int(h * .62), int(w * .32):int(w * .72)]
    if patch.size == 0:
        return None
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)

    def read(view, psm):
        pad = cv2.copyMakeBorder(view, 6, 6, 6, 6, cv2.BORDER_CONSTANT,
                                 value=int(np.median(view)))
        up = cv2.resize(pad, None, fx=6, fy=6,
                        interpolation=cv2.INTER_CUBIC)
        raw = ocr_one(up,
                      f'--psm {psm} -c tessedit_char_whitelist=0123456789')
        digits = re.sub(r'[^0-9]', '', raw)
        if not (1 <= len(digits) <= 2):
            return None
        value = int(digits)
        return value if 0 <= value <= 60 else None

    normal = read(gray, 13)
    inverted = read(255 - gray, 13)
    word_normal = read(gray, 8)
    word_inverted = read(255 - gray, 8)
    raw_reads = (normal, inverted, word_normal, word_inverted)

    # First, rescue a visually certain '20' or '22'. The two reference shapes
    # must BOTH match; even if OCR returns only a suffix, no missing digit is
    # guessed from the countdown state or the last visible number.
    geometric = _timer_match_template(img)
    if geometric is not None:
        s = str(geometric)
        if all(x is None or x == geometric or str(x) == s[0] or str(x) == s[-1]
               for x in raw_reads):
            return geometric
        # Contradictory OCR: refuse both the geometric and raw guesses.
        return None

    # For other seconds, trust independent OCR polarities when the shape is
    # consistent. Two visible numbers must never be quietly recorded as one.
    if word_normal is not None and word_normal == word_inverted:
        word = word_normal
        if word >= 10 and _two_digit_glyphs_visible(img):
            if all(x is None or x == word or str(word).endswith(str(x))
                   for x in (normal, inverted)):
                return word
        elif word < 10 and (normal == word or inverted == word):
            if not _two_digit_glyphs_visible(img):
                return word

    # A truncated two-digit read ('20' -> '0', '22' -> '2') is UNKNOWN unless
    # verified by the known templates or full-length OCR consensus.
    if _two_digit_glyphs_visible(img):
        return None
    if normal is not None and normal == inverted:
        return normal
    adaptive = cv2.adaptiveThreshold(gray, 255,
                                     cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY, 11, 1)
    extra = read(adaptive, 13)
    choices = [x for x in (normal, inverted, extra) if x is not None]
    if choices:
        from collections import Counter
        candidate, hits = Counter(choices).most_common(1)[0]
        if hits >= 2:
            return candidate
    return None


def sanitized_header_preview(header):
    """Mask the upper-right user/session code before saving header preview."""
    s=cv2.resize(header,(HEADER_W,HEADER_H))
    s[:,157:]=0
    return s


def capture(sct, rect):
    return cv2.cvtColor(np.asarray(sct.grab(rect)),cv2.COLOR_BGRA2BGR)


def find_monitor_for_score(monitors, reg):
    x=reg['left']+reg['width']/2
    y=reg['top']+reg['height']/2
    for m in monitors[1:]:
        if m['left'] <= x < m['left']+m['width'] and m['top']<=y<m['top']+m['height']:
            return m
    raise RuntimeError('Score region no longer on any available monitor. Check screen arrangement / original calibration.')


def minimize_console():
    if os.name=='nt':
        try:
            import ctypes
            hwnd=ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd,6)
                return hwnd
        except Exception: pass
    return None


def restore_console(hwnd):
    if hwnd and os.name=='nt':
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(hwnd,9)
        except Exception: pass


def select_regions():
    import mss
    print('One calibration for DEALER/TABLE header and GREEN TIMER. Original score ROI is reused.')
    print('Open RB02, keep dealer header + GREEN countdown visible. Do NOT include browser URL/token.')
    print('This captures one static screenshot of the same monitor as your original score ROI.')
    print('When the selection window appears: drag, then Enter. ESC cancels.')
    input('Press ENTER to capture screen and select two areas...')
    old=load_score_region()
    hwnd=minimize_console()
    try:
        time.sleep(1.3)
        with mss.mss() as sct:
            mon=find_monitor_for_score(sct.monitors, old)
            shot=capture(sct,mon)
    finally:
        restore_console(hwnd)
    h,w=shot.shape[:2]
    ratio=min(1.0,1600/w,900/h)
    display=cv2.resize(shot,(round(w*ratio),round(h*ratio)))
    labels=[('header','SELECT TOP-RIGHT DEALER + TABLE HEADER, ENTER'),('timer','SELECT THE WHOLE GREEN CIRCULAR TIMER, ENTER')]
    new={}
    try:
        for key,title in labels:
            roi=cv2.selectROI(title,display,showCrosshair=True,fromCenter=False)
            cv2.destroyWindow(title)
            x,y,rw,rh=roi
            if rw < 35 or rh < 16:
                raise RuntimeError('Selection canceled or too small. No previous configuration was overwritten.')
            new[key]={
                'left':mon['left']+round(x/ratio), 'top':mon['top']+round(y/ratio),
                'width':round(rw/ratio),'height':round(rh/ratio)
            }
    finally:
        cv2.destroyAllWindows()
    write_json_safe(V4_CONFIG,{'version':4,'created_at':clock(),'header':new['header'],'timer':new['timer'],
        'note':'Original RB02_REGION.json contains score bar; do not move/resize Chrome after calibration.'})
    print('[OK] Saved extra regions:',V4_CONFIG)
    # Preview only the cropped ROIs. Mask user/session code in upper right.
    with mss.mss() as sct:
        h_img=capture(sct,new['header']); t_img=capture(sct,new['timer'])
    sample_dir=OUTPUT/'previews'; sample_dir.mkdir(parents=True,exist_ok=True)
    hpath=score.write_png_unicode(sample_dir/('header_sanitized_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.png'),sanitized_header_preview(h_img))
    tpath=score.write_png_unicode(sample_dir/('timer_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.png'),t_img)
    print('[OK] Header preview (ID region masked):',hpath)
    print('[OK] Timer preview:',tpath)
    print('[CHECK] Header OCR:',parse_header(h_img))
    print('[CHECK] Displayed timer:',parse_timer(t_img))
    print('If either crop is wrong, rerun CALIBRATE_DG_HEADER_TIMER.cmd. Keep score ROI unchanged.')


class Context:
    def __init__(self, output, wanted='RB02'):
        self.output=Path(output)
        self.output.mkdir(parents=True,exist_ok=True)
        self.wanted=wanted
        self.dealer_samples=deque(maxlen=5)
        self.table_samples=deque(maxlen=5)
        self.dealer=None
        self.table=None
        self.header_at=None
        self.header_tick=-1e12
        self.timer=None
        self.timer_at=None
        self.timer_tick=-1e12
        self.last_timer_logged=None
        self.last_signal=-1e12
        self.last_timer_change=-1e12
        self.previous_status=None
        self.status='STARTING'
        self.last_status_written=-1e12
        self.alert_emitted=False
        self.has_ever_valid=False
        self.events=self.output/'DG_RB02_EVENTS.jsonl'

    def apply_header(self,result,tick):
        if result.get('table'):
            self.table_samples.append(result['table'])
            if len(self.table_samples)>=2 and self.table_samples[-1]==self.table_samples[-2]:
                self.table=self.table_samples[-1]
                self.header_tick=tick; self.header_at=clock()
                self.last_signal=tick
                self.has_ever_valid=True
        if result.get('dealer'):
            self.dealer_samples.append(result['dealer'])
            if len(self.dealer_samples)>=2 and self.dealer_samples[-1].lower()==self.dealer_samples[-2].lower():
                self.dealer=self.dealer_samples[-1]

    def apply_timer(self,value,tick):
        if value is None:
            return
        self.timer=value
        self.timer_tick=tick
        self.timer_at=clock()
        self.last_signal=tick
        self.has_ever_valid=True
        if value != self.last_timer_logged:
            self.last_timer_logged=value
            self.last_timer_change=tick
            append_jsonl(self.events,{'type':'displayed_countdown','at':self.timer_at,'displayed_seconds':value,'official_timer':False})
            print('[countdown] display=',value,'(screen reading; not an official settlement clock)')

    def recent_header_ok(self,tick,within=8):
        return self.table==self.wanted and tick-self.header_tick < within

    def get_event_metadata(self,tick):
        table=self.table if tick-self.header_tick < 30 else None
        dealer=self.dealer if tick-self.header_tick < 30 else None
        timer=self.timer if tick-self.timer_tick < 15 else None
        return {
            'table_detected':table,'table_source':'screen_ocr' if table else 'configured_label_only',
            'dealer':dealer,'dealer_source':'screen_ocr_unverified' if dealer else None,
            'timer_last_seen':timer,'timer_last_seen_at':self.timer_at if timer is not None else None
        }

    def update_health(self,tick,*,stale_seconds=90,session_freeze_seconds=20):
        # NEVER infer official login status from the absence of one visual feature.
        age=tick-self.last_signal
        if not self.has_ever_valid:
            state='WAITING_FOR_VALID_SCREEN'
        elif self.table and self.table!=self.wanted and tick-self.header_tick < 12:
            state='WRONG_TABLE_CHECK_MANUALLY'
        elif age > stale_seconds:
            state='NEEDS_MANUAL_PAGE_CHECK_OR_RELOGIN'
        elif self.timer is not None and tick-self.timer_tick < 3 and tick-self.last_timer_change > session_freeze_seconds and age<20:
            state='POSSIBLE_FROZEN_COUNTDOWN_CHECK_PAGE'
        else:
            state='OBSERVING'
        self.status=state
        if state != self.previous_status:
            print('[status]',state)
            append_jsonl(self.events,{'type':'observer_status','at':clock(),'status':state})
            self.previous_status=state
            if state.startswith('NEEDS_') and not self.alert_emitted:
                self.alert_emitted=True
                if os.name=='nt':
                    try:
                        import winsound
                        winsound.MessageBeep()
                    except Exception: pass
            elif state=='OBSERVING':
                self.alert_emitted=False
        if tick-self.last_status_written >=5:
            write_json_safe(self.output/'DG_RB02_STATUS.json',{
                'updated_at':clock(),'observer_status':state,
                'latest_table_detected':self.table if tick-self.header_tick<30 else None,
                'latest_dealer':self.dealer if tick-self.header_tick<30 else None,
                'displayed_timer':self.timer if tick-self.timer_tick<15 else None,
                'last_timer_observed_at':self.timer_at if tick-self.timer_tick<15 else None,
                'last_visual_signal_age_seconds':round(max(0,age),1) if self.has_ever_valid else None,
                'warning':'No page-login status is independently validated. A 404, expiry, closed tab, covered screen or layout change all require manual check.'
            })
            self.last_status_written=tick


class IntegratedScoreRecorder(score.OverlayRecorder):
    """Reuse tested V3 score OCR; enrich only accepted *candidates*."""
    def __init__(self,context,output,*,min_spacing=12,ocr_every=0.65):
        super().__init__(output,ocr_every=ocr_every)
        self.context=context
        self.last_saved_tick=-1e12
        self.min_spacing=min_spacing

    def finish(self):
        result=self._stable_vote()
        now=time.monotonic()
        if result is None:
            self.skipped+=1
            print('[skip] unstable final score. No record written.')
            append_jsonl(self.context.events,{'type':'skipped_score','at':clock(),'reason':'unstable_final_score'})
            return
        if not self.context.recent_header_ok(now,within=9):
            self.skipped+=1
            print('[skip] RB02 header not recently confirmed; possible tab switch/timeout/wrong table')
            append_jsonl(self.context.events,{'type':'skipped_score','at':clock(),'reason':'table_not_confirmed'})
            return
        if now-self.last_saved_tick < self.min_spacing:
            self.skipped+=1
            print('[skip] duplicate-risk: another candidate recorded within',self.min_spacing,'seconds')
            append_jsonl(self.context.events,{'type':'skipped_score','at':clock(),'reason':'too_close_to_prior_candidate'})
            return
        p,b=result['pair']
        self.number+=1
        cid=f'rb02v4-{self.session}-{self.number:04d}'
        filename=cid+'.png'
        evidence=self.output/'evidence'/filename
        actual=score.write_png_unicode(evidence,result['reading']['strip'])
        when=clock()
        meta=self.context.get_event_metadata(now)
        lag_ms=max(0,round((now-self.last_visible_time)*1000)) if self.last_visible_time is not None else None
        row={
            'candidate_id':cid,'table':'RB02',**meta,
            'player_points':p,'banker_points':b,
            'winner':'PLAYER' if p>b else 'BANKER' if b>p else 'TIE',
            'score_seen_at':self.last_visible_stamp,'saved_at':when,
            'post_overlay_delay_ms':lag_ms,
            'round_id':None,'settled_at':None,'verified':False,
            'agreement_samples':result['agreement'],
            'evidence_image':f'evidence/{filename}' if actual==evidence else str(actual),
            'source':'local_visible_scoreboard_ocr',
            'flags':'SCREEN_CANDIDATE_NOT_OFFICIAL'
        }
        append_jsonl(self.output/'DG_RB02_CANDIDATES.jsonl',row)
        csvfile=self.output/'DG_RB02_CANDIDATES.csv'
        fresh=not csvfile.exists() or csvfile.stat().st_size==0
        with csvfile.open('a',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=CSV_FIELDS,extrasaction='ignore')
            if fresh: writer.writeheader()
            writer.writerow(row)
        append_jsonl(self.context.events,{'type':'score_candidate','at':when,'candidate_id':cid,
                             'table':row['table'],'player_points':p,'banker_points':b,
                             'dealer':row['dealer'],'post_overlay_delay_ms':lag_ms,'verified':False})
        self.last_saved_tick=now
        self.hits+=1
        print(f'[CANDIDATE] P={p} B={b} {row["winner"]}; dealer={row["dealer"] or "unknown"}, '
              f'table={row["table_detected"]}, observed={row["score_seen_at"]}, '
              f'capture_delay={lag_ms}ms, saved={self.hits}')


@contextmanager
def single_instance_lock(directory):
    """Prevent duplicate local V4 observers writing simultaneous candidates."""
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    f=(directory/'_v4_observer.lock').open('a+b')
    locked=False
    try:
        try:
            if os.name=='nt':
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            locked=True
        except OSError:
            raise RuntimeError('Another DG RB02 V4 monitor is already running. Stop the other instance first.')
        yield
    finally:
        if locked:
            try:
                if os.name=='nt':
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(),fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def monitor(args):
    with single_instance_lock(OUTPUT):
        return _monitor_locked(args)


def _monitor_locked(args):
    import mss
    regions=get_regions()
    OUTPUT.mkdir(parents=True,exist_ok=True)
    print('DG RB02 LOCAL V4 (READ-ONLY) - SCORE + DEALER/TABLE + DISPLAYED TIMER')
    print('No login/404 bypass, no fake bets, no background browser automation.')
    print('Only visible desktop regions; NOT the DG official results API. Ctrl+C to stop.')
    print('[config] score=',regions['score'],'header=',regions['header'],'timer=',regions['timer'])
    context=Context(OUTPUT)
    score_rec=IntegratedScoreRecorder(context,OUTPUT,min_spacing=args.min_round_spacing,ocr_every=args.score_ocr_every)
    # A separate metadata worker makes timer/header OCR nonblocking.
    executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='dg-meta')
    header_pending=None
    timer_pending=None
    next_header=0
    next_timer=0
    next_print=0
    try:
        with mss.mss() as sct:
            while True:
                tick=time.monotonic()
                # Poll existing OCR tasks and collect results without stalling the 0.25s score capture.
                if header_pending is not None and header_pending.done():
                    try:
                        context.apply_header(header_pending.result(),tick)
                    except Exception as e:
                        print('[warn] header OCR:',type(e).__name__)
                    header_pending=None
                if timer_pending is not None and timer_pending.done():
                    try:
                        context.apply_timer(timer_pending.result(),tick)
                    except Exception as e:
                        print('[warn] countdown OCR:',type(e).__name__)
                    timer_pending=None
                if header_pending is None and tick>=next_header:
                    header_img=capture(sct,regions['header'])
                    header_pending=executor.submit(parse_header,header_img)
                    next_header=tick+args.header_interval
                if timer_pending is None and tick>=next_timer:
                    timer_img=capture(sct,regions['timer'])
                    timer_pending=executor.submit(parse_timer,timer_img)
                    next_timer=tick+args.timer_interval
                strip=capture(sct,regions['score'])
                current=score_rec.feed(strip,clock(),tick=tick)
                if current:
                    # A visual score bar alone is not proof of the correct game tab.
                    pass
                context.update_health(tick,stale_seconds=args.stale_seconds)
                if tick>next_print:
                    print(f'[running] status={context.status}, dealer={context.dealer or "?"}, '
                          f'table={context.table or "?"}, timer={context.timer}, '
                          f'saved={score_rec.hits}, skips={score_rec.skipped}')
                    next_print=tick+15
                time.sleep(max(0.0,args.interval-(time.monotonic()-tick)))
    except KeyboardInterrupt:
        print('\n[stopped] Saved candidates:',score_rec.hits,'skipped:',score_rec.skipped)
    finally:
        score_rec.close()
        executor.shutdown(wait=False,cancel_futures=True)


def test_samples():
    # Decode from bytes so Windows paths with non-ASCII characters are supported.
    # cv2.imread(path) can fail for a Chinese directory even when the PNG exists.
    def read_sample(path):
        path = Path(path)
        raw = path.read_bytes()
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('Unable to decode sample PNG: ' + str(path))
        return image

    header=read_sample(SAMPLES/'HEADER_Sandy_RB02.png')
    timer=read_sample(SAMPLES/'TIMER_22.png')
    if header is None or timer is None:
        raise RuntimeError('Missing bundled sample images')
    print('[SAMPLE] header:',parse_header(header))
    print('[SAMPLE] countdown:',parse_timer(timer))
    h=parse_header(header); t=parse_timer(timer)
    assert h['dealer']=='Sandy',f'dealer misread: {h}'
    assert h['table']=='RB02',f'table misread: {h}'
    assert t==22,f'countdown misread: {t}'
    print('[PASS] Both user-supplied screenshot samples parsed. Not proof of live layout accuracy.')


def main():
    set_display_dpi()
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--test-samples',action='store_true')
    ap.add_argument('--calibrate-extras',action='store_true')
    ap.add_argument('--monitor',action='store_true')
    ap.add_argument('--interval',type=float,default=0.25)
    ap.add_argument('--timer-interval',type=float,default=1.0)
    ap.add_argument('--header-interval',type=float,default=4.0)
    ap.add_argument('--score-ocr-every',type=float,default=0.65)
    ap.add_argument('--stale-seconds',type=float,default=90.0)
    ap.add_argument('--min-round-spacing',type=float,default=12.0)
    args=ap.parse_args()
    if args.test_samples:
        test_samples()
    elif args.calibrate_extras:
        select_regions()
    elif args.monitor:
        if not 0.20<=args.interval<=2: ap.error('--interval out of range')
        if not 0.40<=args.score_ocr_every<=2: ap.error('--score-ocr-every out of range')
        if not 0.7<=args.timer_interval<=10: ap.error('--timer-interval out of range')
        if not 2<=args.header_interval<=30: ap.error('--header-interval out of range')
        monitor(args)
    else:
        ap.print_help()


if __name__=='__main__':
    main()
