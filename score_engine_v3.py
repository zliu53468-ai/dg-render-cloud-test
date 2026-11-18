#!/usr/bin/env python3
"""DG RB02 screen-overlay candidate recorder, local/read-only.

Captures a user-selected *visible* scoreboard rectangle. Does not interact with
sites, access cookies, decode web sockets, place wagers, or claim official data.
"""
import argparse
import csv
import json
import os
import tempfile
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

try:
    import cv2
    import numpy as np
    import pytesseract
except ImportError as e:
    sys.exit('Missing Python package: ' + str(e) + '\nRun INSTALL_LIVE_PACKAGES.cmd')

if os.name == 'nt':
    tesseract = Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe')
    if tesseract.exists():
        pytesseract.pytesseract.tesseract_cmd = str(tesseract)
    # Avoid Windows display scaling changing physical screenshot coordinates.
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'RB02_REGION.json'
OUTPUT = ROOT / 'RB02_LIVE_OUTPUT'
REF_W, REF_H = 420, 58
# Wider bounding boxes retain full digits and whitespace for Tesseract.
# Keep these tied to the normalized 420x58 overlay, not full-screen positions.
REF_PLAYER = (123, 5, 174, 55)
REF_BANKER = (257, 5, 314, 55)
REF_RED = (332, 15, 420, 56)
CSV_KEYS = ['candidate_id', 'table', 'player_points', 'banker_points', 'winner',
            'observed_at', 'settled_at', 'round_id', 'dealer', 'verified',
            'agreement_samples', 'source', 'evidence_image']


def clock():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def write_png_unicode(path, bgr_image):
    """Save only the cropped PNG. Fall back on Windows if the target is unwritable.

    Some machines reject writes to an existing preview due to a file lock or
    filesystem integration. We report the *actual* save location, never claim
    success if it could not be written.
    """
    destination = Path(path)
    ok, encoded = cv2.imencode('.png', bgr_image)
    if not ok:
        raise RuntimeError('Cannot encode screenshot as PNG')
    data = encoded.tobytes()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        actual = destination
    except OSError as original_error:
        fallback_dir = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'DG_RB02_Screen_Results'
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback_name = (destination.stem + '_fallback_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.png')
            actual = fallback_dir / fallback_name
            actual.write_bytes(data)
        except OSError as fallback_error:
            raise RuntimeError(
                f'Could not write PNG at {destination}: {original_error}; '
                f'fallback {fallback_dir} also failed: {fallback_error}'
            ) from fallback_error
        print('[INFO] Original PNG path could not be written:', original_error)
        print('[INFO] Saved PNG to alternative Windows folder instead:', actual)
    if not actual.is_file() or actual.stat().st_size == 0:
        raise RuntimeError('PNG write did not create a nonempty file: ' + str(actual))
    return actual


def new_preview_path():
    # Unique name avoids overwriting the PNG still open in Windows Photos.
    return ROOT / ('RB02_SCOREBAR_PREVIEW_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.png')


def show_preview(path):
    if os.name == 'nt':
        try:
            os.startfile(str(path))
        except OSError as exc:
            print('[INFO] Could not open Photos automatically:', exc)
            print('[INFO] Open the image manually at:', path)


def rect_crop(image, rect):
    x1, y1, x2, y2 = rect
    return image[y1:y2, x1:x2]


def normalized_strip(frame):
    return cv2.resize(frame, (REF_W, REF_H), interpolation=cv2.INTER_LINEAR)


def banner_visible(strip):
    piece = rect_crop(strip, REF_RED)
    if piece.size == 0:
        return False
    hsv = cv2.cvtColor(piece, cv2.COLOR_BGR2HSV)
    red = ((hsv[:, :, 0] < 17) | (hsv[:, :, 0] > 170))
    mask = red & (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 25)
    return float(mask.mean()) > 0.27


def recognize_digit(strip, rect):
    """Try a high-contrast OCR pass; retry with Otsu only if uncertain.

    A generous black border makes individual digits recognizable without
    running the six OCR subprocess calls per screenshot used in V2.
    """
    patch = rect_crop(strip, rect)
    if patch.size == 0:
        return None, 0.0
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    candidates = (
        cv2.threshold(gray, 145, 255, cv2.THRESH_BINARY)[1],
        cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
    )
    best_number, best_conf = None, -1.0
    for image in candidates:
        image = cv2.copyMakeBorder(image, 15, 15, 15, 15,
                                   cv2.BORDER_CONSTANT, value=0)
        image = cv2.resize(image, (200, 200), interpolation=cv2.INTER_CUBIC)
        try:
            data = pytesseract.image_to_data(
                image, config='--psm 10 -c tessedit_char_whitelist=0123456789',
                output_type=pytesseract.Output.DICT, timeout=2.0)
        except RuntimeError:
            continue
        for raw, conf in zip(data['text'], data['conf']):
            digit = str(raw).strip()
            if len(digit) != 1 or digit not in '0123456789':
                continue
            confidence = max(0.0, float(conf))
            if confidence > best_conf:
                best_number, best_conf = int(digit), confidence
        if best_conf >= 55:  # skip the slower fallback on a clear reading
            break
    return (best_number, round(best_conf, 1)) if best_conf >= 30 else (None, 0.0)


def ocr_pair(strip):
    """OCR a cropped strip; run in a worker so capture stays responsive."""
    p, cp = recognize_digit(strip, REF_PLAYER)
    b, cb = recognize_digit(strip, REF_BANKER)
    return (p, b, cp, cb)


class OverlayRecorder:
    def __init__(self, output, *, ocr_every=0.65, offline=False,
                 max_stale=2.5, recent_seconds=4.0):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / 'evidence').mkdir(exist_ok=True)
        self.session = uuid.uuid4().hex[:10]
        self.number = 0
        self.hits = 0
        self.skipped = 0
        self.episode = None
        self.missing = 0
        self.episode_id = 0
        self.last_visible_time = None
        self.last_visible_stamp = None
        self.readings = []
        self.last_capture = None
        self.pending = None
        self.last_ocr_requested = -1e12
        self.ocr_every = ocr_every
        self.offline = offline
        self.max_stale = max_stale
        self.recent_seconds = recent_seconds
        self.last_announced = None
        self.executor = None if offline else ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix='rb02-ocr')

    def close(self):
        # End without interpreting an unfinished, still-visible scoreboard.
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)

    def _accept_future(self, *, block=False):
        if self.pending is None:
            return
        future, episode_id, at, sec, strip = self.pending
        if not block and not future.done():
            return
        try:
            p, b, cp, cb = future.result(timeout=3.0 if block else 0)
        except FutureTimeout:
            print('[skip] OCR task exceeded timeout')
            self.pending = None
            return
        except Exception as e:
            print('[warn] OCR error:', type(e).__name__, str(e)[:120])
            self.pending = None
            return
        self.pending = None
        if episode_id == self.episode_id and self.episode is not None:
            self._add_reading(at, sec, strip, p, b, cp, cb)

    def _add_reading(self, at, sec, strip, p, b, cp, cb):
        if p is None or b is None:
            return
        self.readings.append({'at': at, 'sec': sec, 'strip': strip,
                              'pair': (p, b), 'confidence': min(cp, cb)})
        self.readings = self.readings[-30:]
        accepted = self._stable_vote()
        if accepted and accepted['pair'] != self.last_announced:
            pair = accepted['pair']
            print(f'[provisional] P={pair[0]} B={pair[1]} (overlay visible; NOT saved yet)')
            self.last_announced = pair

    def _stable_vote(self):
        """Vote only from recent OCR readings; require a consistent tail."""
        if self.last_visible_time is None:
            return None
        latest = self.last_visible_time
        recent = [r for r in self.readings
                  if latest - self.recent_seconds <= r['sec'] <= latest + 0.001]
        if len(recent) < 3:
            return None
        end = recent[-1]
        if latest - end['sec'] > self.max_stale:
            return None  # last good digits too old; could be a mid-deal state
        last_pair = end['pair']
        tail = 0
        for r in reversed(recent):
            if r['pair'] != last_pair:
                break
            tail += 1
        agreement = sum(r['pair'] == last_pair for r in recent[-6:])
        matching = [r for r in recent if r['pair'] == last_pair]
        # No one-off OCR result or single transitional number is accepted.
        if agreement < 3 or tail < 2 or len(matching) < 3:
            return None
        if matching[-1]['sec'] - matching[-3]['sec'] < 0.7:
            return None
        return {'pair': last_pair, 'agreement': agreement, 'reading': end}

    def feed(self, bgr_strip, seen_at, *, tick=None):
        # tick can be video time for reproducible offline regression.
        sec = time.monotonic() if tick is None else float(tick)
        strip = normalized_strip(bgr_strip)
        visible = banner_visible(strip)
        self._accept_future()
        if visible:
            self.missing = 0
            if self.episode is None:
                self.episode_id += 1
                self.episode = True
                self.readings = []
                self.last_announced = None
                self.last_ocr_requested = -1e12
                print('[overlay] score banner detected')
            self.last_visible_time = sec
            self.last_visible_stamp = seen_at
            self.last_capture = strip.copy()
            # Throttled OCR, while the overlay is visible. Only one queued job
            # at any time, with the current scoreboard (no stale queue).
            if sec - self.last_ocr_requested >= self.ocr_every:
                if self.offline:
                    p, b, cp, cb = ocr_pair(strip)
                    self._add_reading(seen_at, sec, strip.copy(), p, b, cp, cb)
                    self.last_ocr_requested = sec
                elif self.pending is None:
                    future = self.executor.submit(ocr_pair, strip.copy())
                    self.pending = (future, self.episode_id, seen_at, sec, strip.copy())
                    self.last_ocr_requested = sec
        elif self.episode is not None:
            self.missing += 1
            if self.missing >= 3:
                # Incorporate the last in-flight OCR result before voting.
                self._accept_future(block=True)
                self.finish()
                self.episode = None
                self.missing = 0
                self.last_capture = None
        return visible

    def finish(self):
        result = self._stable_vote()
        if result is None:
            self.skipped += 1
            print('[skip] no recent stable final score (review manually)')
            return
        (p, b) = result['pair']
        agree = result['agreement']
        self.number += 1
        winner = 'PLAYER' if p > b else 'BANKER' if b > p else 'TIE'
        candidate_id = f'rb02-{self.session}-{self.number:04d}'
        filename = f'{candidate_id}.png'
        evidence = self.output / 'evidence' / filename
        # Evidence is from a frame with recognized digits, not an unrelated UI.
        actual_evidence = write_png_unicode(evidence, result['reading']['strip'])
        record = {
            'candidate_id': candidate_id,
            'table': 'RB02',
            'player_points': p, 'banker_points': b, 'winner': winner,
            'observed_at': self.last_visible_stamp,
            'settled_at': None, 'round_id': None, 'dealer': None,
            'verified': False, 'agreement_samples': agree,
            'source': 'local_visible_screen_overlay',
            'evidence_image': (f'evidence/{filename}' if actual_evidence == evidence else str(actual_evidence)),
            'warning': 'Unverified score candidate; local observation is NOT official settlement time.',
        }
        jsonl = self.output / 'DG_RB02_LIVE_CANDIDATES.jsonl'
        with jsonl.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        csvpath = self.output / 'DG_RB02_LIVE_CANDIDATES.csv'
        fresh = not csvpath.exists() or csvpath.stat().st_size == 0
        with csvpath.open('a', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_KEYS, extrasaction='ignore')
            if fresh:
                writer.writeheader()
            writer.writerow(record)
        self.hits += 1
        print(f'[CANDIDATE ONLY] {candidate_id}: PLAYER={p}, BANKER={b}, '
              f'{winner}, agreement={agree}, seen={self.last_visible_stamp}')
        print('[saved]', csvpath)


def get_screenshot_tool():
    try:
        import mss
    except ImportError:
        sys.exit('Missing screen capture package mss. Run INSTALL_LIVE_PACKAGES.cmd')
    return mss.mss()


def snapshot(sct, rect):
    from_mss = np.asarray(sct.grab(rect))
    return cv2.cvtColor(from_mss, cv2.COLOR_BGRA2BGR)


def minimize_console_for_capture():
    if os.name != 'nt':
        return None
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            return hwnd
    except Exception:
        pass
    return None


def restore_console(hwnd):
    if hwnd is not None:
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        except Exception:
            pass


def calibrate(args):
    print('CALIBRATION: Open DG RB02 in Chrome. Keep the scoreboard visible.')
    print('On the screenshot, drag a rectangle around ONLY the full bar: [閒 number VS number 莊].')
    print('Press ENTER or SPACE to confirm, ESC to cancel. Do not select the whole browser.')
    print('The CMD window will minimize briefly to avoid covering the game.')
    sys.stdout.flush()
    hwnd = minimize_console_for_capture()
    try:
        time.sleep(1.1)
        with get_screenshot_tool() as sct:
            monitors = sct.monitors[1:]
            if args.monitor < 1 or args.monitor > len(monitors):
                sys.exit(f'Invalid monitor number; available monitors: 1..{len(monitors)}')
            mon = monitors[args.monitor - 1]
            shot = snapshot(sct, mon)
    finally:
        restore_console(hwnd)
    h, w = shot.shape[:2]
    ratio = min(1.0, 1380 / w, 780 / h)
    preview = cv2.resize(shot, (round(w * ratio), round(h * ratio)))
    try:
        roi = cv2.selectROI('DRAG AROUND SCORE BAR - press ENTER', preview,
                            showCrosshair=True, fromCenter=False)
        cv2.destroyAllWindows()
    except cv2.error as e:
        sys.exit('Cannot open preview window: ' + str(e))
    x, y, rw, rh = roi
    if rw < 70 or rh < 10:
        sys.exit('No area selected; calibration canceled.')
    area = {
        'left': mon['left'] + round(x / ratio),
        'top': mon['top'] + round(y / ratio),
        'width': round(rw / ratio), 'height': round(rh / ratio)
    }
    if not 5.0 <= area['width'] / area['height'] <= 10.0:
        print('WARNING: unusual aspect ratio. Expected a long, narrow blue/red score bar.')
    config = {'version': 1, 'screen_region': area,
              'description': 'Only blue/red PLAYER VS BANKER scoreboard bar; visible on Windows desktop',
              'saved_at': clock()}
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    print('[saved]', CONFIG)
    with get_screenshot_tool() as sct:
        strip = normalized_strip(snapshot(sct, area))
    result = write_png_unicode(new_preview_path(), strip)
    print('[OK] Selected SCORE BAR preview:', result)
    show_preview(result)
    print('If it does not look like [閒 number VS number 莊], recalibrate.')


def preview_existing_region():
    if not CONFIG.is_file():
        sys.exit('RB02_REGION.json not found. Run CALIBRATE_RB02.cmd first.')
    conf = json.loads(CONFIG.read_text(encoding='utf-8'))
    area = conf.get('screen_region', {})
    if not all(isinstance(area.get(k), int) for k in ('left', 'top', 'width', 'height')):
        sys.exit('Invalid RB02_REGION.json. Recalibrate.')
    if area['width'] < 60 or area['height'] < 10:
        sys.exit('The selected region is too small. Recalibrate.')
    print('Using saved region:', area)
    print('Keep the DG scoreboard uncovered on screen.')
    # Hide the console briefly so it cannot cover the selected region.
    sys.stdout.flush()
    hwnd = minimize_console_for_capture()
    try:
        time.sleep(1.2)
        with get_screenshot_tool() as sct:
            strip = normalized_strip(snapshot(sct, area))
    finally:
        restore_console(hwnd)
    result = write_png_unicode(new_preview_path(), strip)
    print('[OK] Preview created:', result)
    show_preview(result)
    print('If this image does not show the blue/red bar, recalibrate with DG visible.')


def monitor(args):
    if not CONFIG.exists():
        sys.exit('RB02_REGION.json is missing. Double-click CALIBRATE_RB02.cmd first.')
    conf = json.loads(CONFIG.read_text(encoding='utf-8'))
    area = conf['screen_region']
    if not all(isinstance(area.get(k), int) for k in ('left', 'top', 'width', 'height')):
        sys.exit('Invalid region file; recalibrate.')
    if area['width'] < 60 or area['height'] < 10:
        sys.exit('Region too small; recalibrate.')
    print('DG RB02 VISIBLE SCREEN CANDIDATE MONITOR. READ ONLY. NO BETTING OR SITE CONTROL.')
    print('Keep Chrome visible and scoreboard unobstructed. Do NOT minimize or cover it.')
    print('Only a cropped score strip is ever saved; no full-screen screenshots.')
    print('All results remain UNVERIFIED until official round record matching.')
    print('Press Ctrl+C to stop. Output:', OUTPUT)
    rec = OverlayRecorder(OUTPUT, ocr_every=args.ocr_every)
    print('Screen area:', area)
    print(f'[mode] Capture every {args.interval:.2f}s; OCR approx every {args.ocr_every:.2f}s while visible.')
    print('[mode] Final candidates are written only after the score banner disappears.')
    try:
        with get_screenshot_tool() as sct:
            last_msg = time.monotonic()
            while True:
                started = time.monotonic()
                strip = snapshot(sct, area)
                current = rec.feed(strip, clock(), tick=started)
                if started - last_msg >= 15:
                    print(f'[running] visible_banner={current}, candidates_saved={rec.hits}, skipped={rec.skipped}')
                    last_msg = started
                pause = max(0.0, args.interval - (time.monotonic() - started))
                time.sleep(pause)
    except KeyboardInterrupt:
        print('\n[stopped] Saved candidates:', rec.hits, 'Skipped:', rec.skipped)
    finally:
        rec.close()


def check_video(args):
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        sys.exit('Cannot open demo video.')
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    every = max(1, round(fps / 2))
    print('Offline regression mode (not live). Using recorded sample region.')
    rec = OverlayRecorder(Path(args.out), offline=True, ocr_every=0.8)
    frameid = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frameid % every == 0:
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = (147, 36, 567, 94)
            strip = frame[round(y1 * h / 936):round(y2 * h / 936),
                          round(x1 * w / 1670):round(x2 * w / 1670)]
            seconds = frameid / fps
            rec.feed(strip, f'video-second:{seconds:.2f}', tick=seconds)
        frameid += 1
    cap.release()
    rec.close()
    print('Completed. Unverified candidate records:', rec.hits, 'Skipped:', rec.skipped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--calibrate', action='store_true')
    ap.add_argument('--preview', action='store_true', help='Recreate PNG from saved region')
    ap.add_argument('--monitor', type=int, default=1, help='Monitor number, starting from 1')
    ap.add_argument('--interval', type=float, default=0.25)
    ap.add_argument('--ocr-every', type=float, default=0.65, help='OCR interval while score is visible')
    ap.add_argument('--demo-video', dest='video', default=None)
    ap.add_argument('--out', default=str(OUTPUT))
    args = ap.parse_args()
    if args.calibrate:
        calibrate(args)
    elif args.preview:
        preview_existing_region()
    elif args.video:
        check_video(args)
    else:
        if args.interval < 0.20:
            sys.exit('Minimum capture interval is 0.20 seconds')
        if not 0.40 <= args.ocr_every <= 2.0:
            sys.exit('--ocr-every must be between 0.40 and 2.0 seconds')
        monitor(args)


if __name__ == '__main__':
    main()
