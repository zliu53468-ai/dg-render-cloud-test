#!/usr/bin/env python3
"""Standalone, token-protected Render Chromium visual observer.

Launches ONLY on an explicit authorized operator command. Browser gestures are
manual and never synthesized to maintain activity. A DG timeout is not bypassed.
All visual OCR results are unverified; no banking/betting logic is included.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from queue import Queue, Empty, Full
import hmac
import json
import os
import re
import signal
import threading
import time

import cv2
import numpy as np

from cloud_ocr import CloudOCR, REQUIRED, normalized_roi, decode_jpeg, last_candidates

ROOT=Path(__file__).resolve().parent
OUTPUT=Path(os.getenv('DG_STORAGE_DIR','/tmp/dg-cloud-observer')).resolve()
OUTPUT.mkdir(parents=True,exist_ok=True)
ROIS=OUTPUT/'rois.json'
PORT=int(os.getenv('PORT','10000'))
AUTH_TOKEN=os.getenv('DG_CONTROL_TOKEN','')
START_URL=os.getenv('DG_START_URL','https://dg66.net/Opengame/freegame/')
VIEWPORT_W=max(640,min(1920,int(os.getenv('DG_VIEWPORT_WIDTH','1600'))))
VIEWPORT_H=max(480,min(1080,int(os.getenv('DG_VIEWPORT_HEIGHT','900'))))
FRAME_INTERVAL=max(.65,min(8.,float(os.getenv('DG_FRAME_INTERVAL_SECONDS','1.25'))))
PREVIEW_INTERVAL=max(.8,min(8.,float(os.getenv('DG_PREVIEW_INTERVAL_SECONDS','2'))))
AUTHORIZED_ACTIONS={'start_browser','stop_browser','go_home','click','type_text','press_key',
                    'start_monitor','stop_monitor','set_roi','capture_once','open_authorized_link'}
ALLOWED_KEYS={'Tab','Enter','Escape','Backspace','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Space'}


def allowed_host(host,allowed):
    host=str(host or '').lower().rstrip('.')
    return any(host==domain or host.endswith('.'+domain) for domain in allowed)


def hosts_from_env():
    default=(urlsplit(START_URL).hostname or '').lower()
    extras=os.getenv('DG_ALLOWED_HOSTS','')
    domains=set([default])
    for x in extras.split(','):
        x=x.strip().lower().rstrip('.')
        if x and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,100}',x):
            domains.add(x)
    return {x for x in domains if x}

ALLOWED_HOSTS=hosts_from_env()


def valid_start_url():
    p=urlsplit(START_URL)
    return (p.scheme=='https' and p.hostname and allowed_host(p.hostname,ALLOWED_HOSTS)
            and not p.username and not p.password and p.port is None
            and not p.query and not p.fragment)



# Operator-pasted one-time links: this is the observed entry form, not a secret
# to place in repository files or startup environment variables.
ENTRY_HOST_PREFIX = 'new-dd-cn.'
ENTRY_PATH = '/ddnewpc/index.html'


def valid_authorized_entry_url(value, allowed=None):
    """Accept only an explicitly operator-supplied HTTPS game-entry URL.

    Accept the short-lived credential solely in an authenticated POST body.
    Never put it in status, errors, application logs, env or persistent files.
    """
    if not isinstance(value, str) or not (1 <= len(value) <= 2048):
        return False
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value):
        return False
    try:
        p = urlsplit(value)
        allow = ALLOWED_HOSTS if allowed is None else allowed
        host = (p.hostname or '').lower().rstrip('.')
        if (p.scheme != 'https' or not host.startswith(ENTRY_HOST_PREFIX) or
                host not in allow or p.port is not None or
                p.username is not None or p.password is not None or
                p.path != ENTRY_PATH or not p.query or p.fragment):
            return False
        q = parse_qs(p.query, keep_blank_values=True, strict_parsing=True,
                     max_num_fields=6)
        if not q or not set(q).issubset({'token', 'type', 'return'}):
            return False
        tok = q.get('token', [])
        if len(tok) != 1 or not re.fullmatch(r'[A-Za-z0-9._~-]{16,256}', tok[0]):
            return False
        types = q.get('type', [])
        if types and (len(types) != 1 or not re.fullmatch(r'[0-9]{1,3}', types[0])):
            return False
        returns = q.get('return', [])
        if returns and (len(returns) != 1 or
                        not re.fullmatch(r'[a-zA-Z0-9.-]{4,128}', returns[0]) or
                        not allowed_host(returns[0], allow)):
            return False
        return True
    except (ValueError, TypeError, UnicodeError):
        return False


def load_rois():
    try:
        obj=json.loads(ROIS.read_text(encoding='utf-8'))
        return {k:normalized_roi(v) for k,v in obj.items() if k in REQUIRED}
    except (OSError,ValueError,TypeError):return {}


def export_preview(raw):
    """Preview is ephemeral, never written. Obscure common account-code region."""
    frame=decode_jpeg(raw)
    h,w=frame.shape[:2]
    # A standardized DG header usually puts a personal account code here.
    # Raw frame is used internally for OCR, but only this masked copy is served.
    cv2.rectangle(frame,(int(w*.90),0),(w-1,int(h*.16)),(18,18,18),-1)
    ok,encoded=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,43])
    return encoded.tobytes() if ok else b''


class CloudOperator:
    def __init__(self):
        self.lock=threading.RLock()
        self.queue=Queue(maxsize=32)
        self.thread=None
        self.preview_jpeg=b''
        self.rois=load_rois()
        self.status={
            'version':'cloud-v1', 'browser':'stopped','page':'not_loaded',
            'monitoring':False,'last_action':'','last_action_result':'not_started',
            'frame_at':None,'preview_at':None,'page_http_error':None,
            'page_host_allowed':False,'page_host':None,'ocr':None,'frames':0,
            'roi_ready':False,'error_code':None,'verified':False,
            'browser_started_at':None,'capture_failures':0,
        }
        self.status['roi_ready']=all(x in self.rois for x in REQUIRED)

    def snapshot(self):
        with self.lock:
            status=dict(self.status)
            active=bool(self.thread and self.thread.is_alive())
            stamp=status.get('frame_at')
            started=status.get('browser_started_at')
            age=max(0,round(time.time()-stamp,1)) if stamp else None
            since_start=max(0,time.time()-started) if started else 0
            stale=(status.get('browser')=='open' and
                   (age>30 if age is not None else since_start>30))
            return {**status,'worker_alive':active,'capture_age_seconds':age,
                    'capture_stale':stale,'rois':dict(self.rois),
                    'storage_ephemeral_by_default':True}

    def preview(self):
        with self.lock:return self.preview_jpeg

    def _set(self,**changes):
        with self.lock:self.status.update(changes)

    def submit(self,action,data=None):
        if action not in AUTHORIZED_ACTIONS:return False
        if self.thread is None or not self.thread.is_alive():
            self.thread=threading.Thread(target=self._loop,name='dg-cloud-playwright',daemon=True)
            self.thread.start()
        try:self.queue.put_nowait((action,data or {}));return True
        except Full:return False

    def _loop(self):
        browser=None;context=None;page=None;ocr=None;playwright=None
        last_frame=0.0;last_preview=0.0;manual_preview=False
        last_doc_error=None;capture_failures=0
        try:
            from playwright.sync_api import sync_playwright
            engine=sync_playwright().start()
            playwright=engine
        except Exception as exc:
            self._set(browser='error',error_code='PLAYWRIGHT_'+type(exc).__name__)
            return

        def current_page():
            nonlocal page
            if context:
                pages=[p for p in context.pages if not p.is_closed()]
                if pages:page=pages[-1]
            return page

        def browser_ready(initial_url=None):
            nonlocal browser,context,page,last_doc_error
            if browser is not None:return
            if initial_url is None:
                if not valid_start_url():raise ValueError('START_URL_REJECTED')
                target = START_URL
            else:
                if not valid_authorized_entry_url(initial_url):
                    raise ValueError('ENTRY_URL_REJECTED')
                target = initial_url
            browser=engine.chromium.launch(headless=True,args=['--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':VIEWPORT_W,'height':VIEWPORT_H},
                                        device_scale_factor=1,accept_downloads=False)
            page=context.new_page()
            self._set(browser='open',page='loading',page_http_error=None,
                      browser_started_at=time.time(),capture_failures=0,
                      error_code=None)
            def on_response(resp):
                nonlocal last_doc_error
                try:
                    if resp.request.is_navigation_request() and resp.frame==resp.frame.page.main_frame:
                        last_doc_error=resp.status if resp.status>=400 else None
                except Exception:pass
            def on_new_page(p):
                p.on('response',on_response)
            context.on('page',on_new_page)
            page.on('response',on_response)
            try:page.goto(target,wait_until='domcontentloaded',timeout=20000)
            except Exception:
                self._set(page='navigation_failed',error_code='PAGE_NAVIGATION_FAILED')
            else:self._set(page='loaded')

        try:
            while True:
                for i in range(4):
                    try:action,params=self.queue.get_nowait()
                    except Empty:break
                    result='ok'
                    self._set(last_action=action,last_action_result='working')
                    try:
                        if action=='start_browser':
                            browser_ready()
                        elif action=='stop_browser':
                            if ocr:ocr.close();ocr=None
                            if context:context.close();context=None
                            if browser:browser.close();browser=None
                            page=None
                            self._set(browser='stopped',page='not_loaded',monitoring=False,
                                      ocr=None,page_host_allowed=False,browser_started_at=None,
                                      capture_failures=0)
                        elif action=='open_authorized_link':
                            # Browser interaction is requested by a human using
                            # the protected operator UI, not by automatic login.
                            entry_url = params.get('url')
                            try:
                                if not valid_authorized_entry_url(entry_url):
                                    raise ValueError('ENTRY_URL_REJECTED')
                                if ocr:
                                    ocr.close();ocr=None;self._set(monitoring=False)
                                last_doc_error=None
                                if browser:
                                    current_page().goto(entry_url,wait_until='domcontentloaded',timeout=20000)
                                    self._set(page='loaded')
                                else:
                                    browser_ready(initial_url=entry_url)
                            finally:
                                # Do not retain the bearer URL in the command queue.
                                params['url']=''
                                entry_url=None
                        elif action=='go_home':
                            if ocr:ocr.close();ocr=None;self._set(monitoring=False)
                            if not browser: raise RuntimeError('BROWSER_NOT_STARTED')
                            last_doc_error=None
                            current_page().goto(START_URL,wait_until='domcontentloaded',timeout=20000)
                            self._set(page='loaded')
                        elif action in ('click','type_text','press_key'):
                            if not browser:raise RuntimeError('BROWSER_NOT_STARTED')
                            if ocr:raise RuntimeError('STOP_MONITOR_BEFORE_OPERATOR_INPUT')
                            p=current_page()
                            if not allowed_host(urlsplit(p.url).hostname,ALLOWED_HOSTS):
                                raise ValueError('PAGE_HOST_NOT_ALLOWED')
                            if action=='click':
                                x=float(params.get('x',-1));y=float(params.get('y',-1))
                                if not (0<=x<=VIEWPORT_W and 0<=y<=VIEWPORT_H):
                                    raise ValueError('CLICK_OUT_OF_BOUNDS')
                                p.mouse.click(x,y)
                            elif action=='type_text':
                                value=params.get('text','')
                                if not isinstance(value,str) or len(value)>256:
                                    raise ValueError('INVALID_INPUT_LENGTH')
                                p.keyboard.insert_text(value)
                                # No logging, retaining, storing, or returning typed input.
                                params['text']=''
                                del value
                            else:
                                key=params.get('key','')
                                if key not in ALLOWED_KEYS:raise ValueError('KEY_NOT_ALLOWED')
                                p.keyboard.press(key)
                        elif action=='set_roi':
                            if ocr:raise RuntimeError('STOP_MONITOR_BEFORE_CALIBRATION')
                            name=params.get('name')
                            if name not in REQUIRED:raise ValueError('UNKNOWN_ROI')
                            roi=normalized_roi(params.get('roi'))
                            with self.lock:
                                self.rois[name]=roi
                                rois_copy=dict(self.rois)
                            ROIS.write_text(json.dumps(rois_copy,indent=2),encoding='utf-8')
                            self._set(roi_ready=all(x in rois_copy for x in REQUIRED))
                        elif action=='start_monitor':
                            if not browser:raise RuntimeError('BROWSER_NOT_STARTED')
                            p=current_page()
                            host=urlsplit(p.url).hostname
                            if not allowed_host(host,ALLOWED_HOSTS):
                                raise ValueError('PAGE_HOST_NOT_ALLOWED')
                            with self.lock:rois_copy=dict(self.rois)
                            if any(k not in rois_copy for k in REQUIRED):
                                raise ValueError('CALIBRATE_4_ROIS_FIRST')
                            if ocr:ocr.close()
                            ocr=CloudOCR(rois_copy,OUTPUT)
                            self._set(monitoring=True,ocr=ocr.snapshot())
                        elif action=='stop_monitor':
                            if ocr:ocr.close();ocr=None
                            self._set(monitoring=False)
                        elif action=='capture_once':manual_preview=True
                    except Exception as exc:
                        msg=str(exc)
                        result=msg if re.fullmatch('[A-Z0-9_]{3,60}',msg) else type(exc).__name__
                        self._set(error_code=result)
                    finally:
                        self._set(last_action_result=result)
                        self.queue.task_done()
                t=time.monotonic()
                if browser and current_page() and not page.is_closed() and (manual_preview or
                        t-last_frame >= (FRAME_INTERVAL if ocr else PREVIEW_INTERVAL)):
                    manual_preview=False
                    host=urlsplit(page.url).hostname
                    safe=allowed_host(host,ALLOWED_HOSTS)
                    self._set(page_host_allowed=safe,page_host=host,
                              page_http_error=last_doc_error)
                    if not safe:
                        if ocr:ocr.close();ocr=None;self._set(monitoring=False)
                        self._set(page='host_not_allowed',error_code='PAGE_HOST_NOT_ALLOWED')
                    else:
                        try:
                            raw=page.screenshot(type='jpeg',quality=72,full_page=False,timeout=15000)
                            image=decode_jpeg(raw)
                            now=time.time()
                            last_frame=t
                            capture_failures=0
                            with self.lock:
                                self.preview_jpeg=export_preview(raw)
                            self._set(frame_at=now,preview_at=now,
                                      capture_failures=0,frames=self.status['frames']+1)
                            if last_doc_error is not None and last_doc_error>=400:
                                if ocr:ocr.close();ocr=None;self._set(monitoring=False)
                                self._set(page='http_error',error_code='PAGE_HTTP_ERROR')
                            elif ocr:
                                try:self._set(ocr=ocr.process(image))
                                except Exception as exc:
                                    self._set(error_code='OCR_'+type(exc).__name__)
                            else:
                                self._set(page='loaded')
                        except Exception as exc:
                            capture_failures+=1
                            self._set(error_code='CAPTURE_'+type(exc).__name__,
                                      capture_failures=capture_failures)
                            if capture_failures>=3:
                                if ocr:
                                    ocr.close();ocr=None
                                    self._set(monitoring=False,ocr=None)
                                self._set(page='capture_failed')
                # Browser events and control actions must run on same Playwright thread.
                time.sleep(.08)
        except Exception as exc:
            # Never log the URL or exception message (which may contain a token).
            self._set(browser='error',page='worker_failed',monitoring=False,
                      error_code='WORKER_'+type(exc).__name__)
        finally:
            for resource in (ocr,context,browser,playwright):
                if resource is None:continue
                try:
                    if resource is playwright:resource.stop()
                    else:resource.close()
                except Exception:pass

SERVICE=CloudOperator()


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):
        # No URL, credentials, browser content or tokens are logged.
        return

    def reply(self,status,payload,*,content_type='application/json; charset=utf-8'):
        if not isinstance(payload,bytes):payload=json.dumps(payload,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(payload)))
        self.send_header('Cache-Control','no-store, private')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers();self.wfile.write(payload)

    def authorized(self):
        if len(AUTH_TOKEN)<32:return False
        token=self.headers.get('Authorization','')
        if not token.startswith('Bearer '):return False
        return hmac.compare_digest(token[7:],AUTH_TOKEN)

    def guard(self):
        if len(AUTH_TOKEN)<32:
            self.reply(503,{'error':'CONTROL_TOKEN_NOT_CONFIGURED'});return False
        if not self.authorized():
            time.sleep(.12)
            self.reply(401,{'error':'UNAUTHORIZED'});return False
        return True

    def do_GET(self):
        path=self.path.split('?',1)[0]
        if path=='/health':
            return self.reply(200,{'ok':True,'mode':'dg-cloud-visual-operator','verified':False})
        if path in ('/','/operator'):
            data=(ROOT/'static'/'operator.html').read_bytes()
            # The operator HTML contains NO secret; every API requires auth.
            return self.reply(200,data,content_type='text/html; charset=utf-8')
        if path in ('/static/operator.js','/static/operator.css'):
            filename=ROOT/'static'/path.rsplit('/',1)[-1]
            mime='text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8'
            return self.reply(200,filename.read_bytes(),content_type=mime)
        if not self.guard():return
        if path=='/api/status':return self.reply(200,SERVICE.snapshot())
        if path=='/api/preview':
            data=SERVICE.preview()
            if not data:return self.reply(404,{'error':'NO_PREVIEW_YET'})
            return self.reply(200,data,content_type='image/jpeg')
        if path=='/api/candidates':return self.reply(200,{'items':last_candidates(OUTPUT)})
        return self.reply(404,{'error':'NOT_FOUND'})

    def do_POST(self):
        if self.path.split('?',1)[0]!='/api/action':
            return self.reply(404,{'error':'NOT_FOUND'})
        if not self.guard():return
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<1 or n>8192:raise ValueError('BODY_TOO_LARGE')
            obj=json.loads(self.rfile.read(n).decode('utf-8'))
            if not isinstance(obj,dict):raise ValueError('BODY_INVALID')
            action=obj.get('action')
            if action not in AUTHORIZED_ACTIONS:raise ValueError('ACTION_UNKNOWN')
            args=obj.get('data') or {}
            if not isinstance(args,dict):raise ValueError('ARGS_INVALID')
            if action=='open_authorized_link' and not valid_authorized_entry_url(args.get('url')):
                raise ValueError('ENTRY_URL_REJECTED')
        except (ValueError,TypeError,UnicodeDecodeError):
            return self.reply(400,{'error':'INVALID_REQUEST'})
        if not SERVICE.submit(action,args):return self.reply(409,{'error':'QUEUE_FULL'})
        return self.reply(202,{'accepted':True,'action':action})


def run():
    print('DG cloud visual operator ready; browser starts on explicit authorized action only.')
    print('No automatic login, session keepalive, betting, or bypass.')
    httpd=ThreadingHTTPServer(('0.0.0.0',PORT),Handler)
    def stop(sig,frame):
        threading.Thread(target=httpd.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:httpd.serve_forever(poll_interval=.25)
    finally:httpd.server_close()

if __name__=='__main__':run()
