import os
import sys
import unittest
import tempfile
from pathlib import Path

import cv2
import numpy as np

HERE=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(HERE))
from cloud_ocr import normalized_roi,crop_viewport,decode_jpeg,CloudOCR
from cloud_app import allowed_host,valid_start_url,valid_authorized_entry_url,export_preview
import dg_rb02_v6 as dg


def png(name):
    return cv2.imdecode(np.frombuffer((HERE/'samples'/name).read_bytes(),dtype=np.uint8), cv2.IMREAD_COLOR)

class Tests(unittest.TestCase):
    def test_rois(self):
        self.assertEqual(normalized_roi({'x':.3,'y':.2,'w':.3,'h':.4})['x'],.3)
        for wrong in [dict(x=-.1,y=0,w=.2,h=.3),dict(x=.8,y=.2,w=.3,h=.3)]:
            with self.assertRaises(ValueError):normalized_roi(wrong)
        image=np.zeros((100,200,3),dtype=np.uint8)
        self.assertEqual(crop_viewport(image,dict(x=.5,y=.25,w=.4,h=.3)).shape[:2],(30,80))

    def test_access(self):
        self.assertTrue(allowed_host('cdn.dg66.net',{'dg66.net'}))
        self.assertFalse(allowed_host('notdg66.net',{'dg66.net'}))
        self.assertTrue(valid_start_url())

    def test_authorized_session_link_is_one_time_only(self):
        allowed={'dg66.net', 'new-dd-cn.20299999.com', 'dggw.vip'}
        fake_token='a' * 32
        base='https://new-dd-cn.20299999.com/ddnewpc/index.html'
        link=base+'?token='+fake_token+'&type=5&return=dggw.vip'
        self.assertTrue(valid_authorized_entry_url(link,allowed))
        new_host='new-dd-cn.ahsy114.com'
        new_link=link.replace('new-dd-cn.20299999.com',new_host)
        self.assertTrue(valid_authorized_entry_url(new_link,allowed|{new_host}))
        self.assertFalse(valid_authorized_entry_url(new_link,allowed))
        self.assertFalse(valid_authorized_entry_url(new_link.replace(new_host,'evil.example'),allowed|{'evil.example'}))
        self.assertFalse(valid_authorized_entry_url(link,{'dg66.net'}))
        for invalid in (
            base, base+'?token=', base+'?type=5',
            'http://new-dd-cn.20299999.com/ddnewpc/index.html?token='+fake_token,
            'https://new-dd-cn.20299999.com:443/ddnewpc/index.html?token='+fake_token,
            'https://user@new-dd-cn.20299999.com/ddnewpc/index.html?token='+fake_token,
            base+'?token='+fake_token+'&return=evil.example',
            base+'?token='+fake_token+'&return=dggw.vip&extra=yes',
            base+'?token='+fake_token+'#fragment',
            'https://new-dd-cn.20299999.com.evil.example/ddnewpc/index.html?token='+fake_token,
        ):
            self.assertFalse(valid_authorized_entry_url(invalid,allowed), invalid.split('?')[0])

    def test_real_samples(self):
        self.assertEqual(dg.parse_header(png('HEADER_HAPPY_RB03.png'))['table'],'RB03')
        self.assertEqual(dg.parse_header(png('HEADER_Sandy_RB02.png'))['table'],'RB02')
        self.assertEqual(dg.parse_timer(png('TIMER_20.png')),20)
        self.assertEqual(dg.parse_timer(png('TIMER_22.png')),22)

    def test_sanitized_preview(self):
        img=np.full((300,500,3),255,dtype=np.uint8)
        jpg=cv2.imencode('.jpg',img)[1].tobytes()
        masked=decode_jpeg(export_preview(jpg))
        self.assertLess(masked[10,480,1],50)
        self.assertGreater(masked[200,50,1],200)

    def test_cloud_session_init(self):
        # Actual cloud Playwright frames need deployment and operator authorization.
        # Test the same normalized ROI path with user-provided OCR samples.
        W,H=1600,900
        inputs={
            'header':png('HEADER_HAPPY_RB03.png'),
            'timer':png('TIMER_20.png'),
            'road':png('ROAD_GRID_EXAMPLE.png'),
            'score':np.zeros((53,413,3),dtype=np.uint8),
        }
        frame=np.full((H,W,3),190,dtype=np.uint8)
        rois={}
        lefts={'header':(1200,60),'timer':(80,30),'road':(100,650),'score':(200,120)}
        for k,v in inputs.items():
            h,w=v.shape[:2]
            x,y=lefts[k]
            self.assertLess(x+w,W)
            self.assertLess(y+h,H)
            frame[y:y+h,x:x+w]=v
            rois[k]=dict(x=x/W,y=y/H,w=w/W,h=h/H)
        with tempfile.TemporaryDirectory() as tmp:
            ocr=CloudOCR(rois,tmp)
            try:
                for _ in range(12):
                    ocr.process(frame)
                    # Header OCR is asynchronous and CPU bound.
                    import time
                    time.sleep(.45)
                state=ocr.snapshot()
                self.assertEqual(state['table'],'RB03')
                self.assertFalse(state['verified'])
                self.assertEqual(state['candidate_count'],0)
            finally:ocr.close()

if __name__=='__main__':unittest.main()
