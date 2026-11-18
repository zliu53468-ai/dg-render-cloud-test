import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HERE=Path(__file__).resolve().parent.parent
TOKEN='not-a-real-key-test-only-'+'A'*32
PORT=19876

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        env=os.environ.copy()
        env.update(PORT=str(PORT),DG_STORAGE_DIR=cls.tmp.name,DG_CONTROL_TOKEN=TOKEN)
        cls.proc=subprocess.Popen([sys.executable,'-u','cloud_app.py'],cwd=HERE,env=env,
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for i in range(50):
            try:
                conn=http.client.HTTPConnection('127.0.0.1',PORT,timeout=1)
                conn.request('GET','/health')
                ok=conn.getresponse().status==200
                conn.close()
                if ok:break
            except (OSError,TimeoutError):time.sleep(.12)
        else:raise RuntimeError('HTTP test server failed to start')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:cls.proc.wait(timeout=6)
        except subprocess.TimeoutExpired:cls.proc.kill()
        cls.tmp.cleanup()

    def get(self,path,token=None):
        conn=http.client.HTTPConnection('127.0.0.1',PORT,timeout=5)
        headers={'Authorization':'Bearer '+token} if token else {}
        conn.request('GET',path,headers=headers)
        resp=conn.getresponse();status=resp.status;data=resp.read();headers=dict(resp.getheaders())
        conn.close();return status,data,headers

    def test_health_public(self):
        code,data,_=self.get('/health')
        self.assertEqual(code,200)
        self.assertTrue(json.loads(data)['ok'])

    def test_token_required(self):
        self.assertEqual(self.get('/api/status')[0],401)
        code,data,_=self.get('/api/status',TOKEN)
        self.assertEqual(code,200)
        self.assertEqual(json.loads(data)['browser'],'stopped')

    def test_static_ui_no_secret(self):
        code,data,headers=self.get('/operator')
        self.assertEqual(code,200)
        self.assertNotIn(TOKEN.encode(),data)
        self.assertIn(b'operator.js',data)
        self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
        self.assertEqual(self.get('/static/operator.js')[0],200)

    def test_reject_unknown_action(self):
        conn=http.client.HTTPConnection('127.0.0.1',PORT,timeout=5)
        payload=json.dumps({'action':'run_hidden_request'}).encode()
        conn.request('POST','/api/action',body=payload,headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'})
        resp=conn.getresponse();self.assertEqual(resp.status,400)
        resp.read();conn.close()

if __name__=='__main__':unittest.main()
