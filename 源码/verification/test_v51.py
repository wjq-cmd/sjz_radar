"""V5.1 timeline and journal integration checks against retained plaintext fixture."""
import bisect, json, subprocess, sys, tempfile, threading, time, unittest, urllib.request
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Parser'))
import gateway_parser as gp
from ErCore.src.core.liveview import confirmed_player_bot
from timeline import Timeline
from container_state import ContainerTracker
FIXTURE=ROOT.parent.parent/'GatewayCapture/verification/live-v4/v4.0.2-field/captures/gateway-20260919-023807-adc7f8c0/realtime.jsonl'
EVENTS=[json.loads(x) for x in FIXTURE.read_text(encoding='utf-8-sig').splitlines()]
def eventually(predicate,timeout=30):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if predicate():return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for state')
def project(engine):
    engine.flush(force=True)
    return {e['key']:{k:e.get(k) for k in ('name','kind','world','hp','team')} for e in engine.last_state['entities']}
class Regression(unittest.TestCase):
    def test_player_bot_requires_multiple_protocol_signals(self):
        self.assertTrue(confirmed_player_bot({'bot':True,'player_id':8001,'uin':8001,'camp':106}))
        self.assertFalse(confirmed_player_bot({'bot':True,'player_id':8001,'uin':8001,'camp':106}, matched_hero=True))
        self.assertFalse(confirmed_player_bot({'bot':True,'player_id':256,'uin':900001,'camp':0}))
        self.assertFalse(confirmed_player_bot({'bot':True,'player_id':256,'camp':106}))
        self.assertFalse(confirmed_player_bot({'bot':False,'player_id':8001,'uin':8001,'camp':106}))

    def test_timeline_pause_seek_and_rebuild(self):
        with tempfile.TemporaryDirectory() as d:
            stop=threading.Event();engine=gp.Engine(Path(d)/'timeline',replay=True,cache_only=True)
            timeline=Timeline(engine,FIXTURE,stop);engine.timeline=timeline
            worker=threading.Thread(target=timeline.run);worker.start()
            try:
                eventually(lambda:not timeline.indexing)
                timeline.control({'seek':['1000']})
                eventually(lambda:timeline.current==len(EVENTS) and not timeline.seeking)
                end=project(engine)
                self.assertEqual(engine.stats['plaintextPackets'],3854)
                self.assertEqual(len(end),30)
                timeline.control({'seek':['0']})
                eventually(lambda:timeline.current==1 and not timeline.seeking)
                self.assertFalse(project(engine))
                before=timeline.position;time.sleep(.08);self.assertEqual(timeline.position,before)
                timeline.control({'speed':['4'],'pause':['0']})
                eventually(lambda:timeline.position>.12);timeline.control({'pause':['1']})
                time.sleep(.02);paused=timeline.position;time.sleep(.08);self.assertEqual(timeline.position,paused)
                timeline.control({'seek':['500']})
                boundary=bisect.bisect_right(timeline.times,timeline.duration*.5)
                eventually(lambda:timeline.current==boundary and not timeline.seeking)
                expected=gp.Engine(Path(d)/'expected',replay=True,cache_only=True)
                try:
                    for event in EVENTS[:boundary]:expected.accept(event)
                    self.assertEqual(project(engine),project(expected))
                finally:expected.close()
                timeline.control({'seek':['1000']})
                eventually(lambda:timeline.current==len(EVENTS) and not timeline.seeking)
                self.assertEqual(project(engine),end)
                self.assertIsNone(timeline.error)
            finally:stop.set();worker.join(10);engine.close()

    def test_journal_burst_partial_line_and_eof_drain(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);journal=root/'input.jsonl';journal.write_bytes(b'')
            p=subprocess.Popen([sys.executable,str(ROOT/'Parser/gateway_parser.py'),'--output',str(root/'out'),'--journal',str(journal),'--port','0','--replay','--cache-only'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                ready=json.loads(p.stdout.readline());port=ready['port']
                def status():
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status',timeout=5) as r:return json.load(r)
                first=json.dumps({'sequence':1,'type':'session_status','data':{}}).encode()
                with journal.open('ab') as f:
                    f.write(first[:15]);f.flush();time.sleep(.05)
                    self.assertEqual(status()['stats'].get('inputEvents',0),0)
                    f.write(first[15:]+b'\n')
                    for i in range(2,12001):
                        f.write((json.dumps({'sequence':i,'type':'session_status','data':{}})+'\n').encode())
                    for i,event in enumerate(EVENTS,12001):
                        f.write((json.dumps(dict(event,sequence=i))+'\n').encode())
                p.stdin.close();p.stdin=None
                output,errors=p.communicate(timeout=40)
                self.assertEqual(p.returncode,0,errors.decode(errors='replace'))
                result=json.loads((root/'out/parser-result.json').read_text())
                self.assertEqual(result['stats']['inputEvents'],12000+len(EVENTS))
                self.assertEqual(result['stats']['plaintextPackets'],3854)
                self.assertEqual(result['stats'].get('eventGaps',0),0)
                self.assertEqual(result['journalReadBytes'],journal.stat().st_size)
                self.assertEqual(result['counts'],{'mate':2,'player':9,'ai':17,'loot':2,'human':11,'bot':17,'characters':11,'charactersIncludingSelf':12,'confirmedBots':0,'suspectedBots':0})
            finally:
                if p.poll() is None:p.terminate();p.wait(10)

    def test_container_lifecycle_unknown_contents(self):
        tracker=ContainerTracker({9:'/Game/BP_LootContainer_Crate.BP_LootContainer_Crate_C'})
        x=SimpleNamespace(open=True,close=False,ch=5,payload=b'\0',pbits=8,hx=False,hm=False,frags=[],seq=1)
        with patch('container_state.sna.parse_prefix',return_value=({'actor':22,'arch':9,'loc':[10.,20.,30.]},0)):
            tracker.consume(x)
        self.assertEqual(tracker.active[22]['world'],[10,20,30])
        self.assertFalse(tracker.active[22]['contents_known'])
        x.open=False;x.close=True;x.close_reason=1;tracker.consume(x)
        self.assertIn(22,tracker.active)
        x.open=True;x.close=False
        with patch('container_state.sna.parse_prefix',return_value=({'actor':22,'arch':9,'loc':[10.,20.,30.]},0)):tracker.consume(x)
        x.open=False;x.close=True;x.close_reason=0;tracker.consume(x)
        self.assertFalse(tracker.active)

if __name__=='__main__':unittest.main(verbosity=2)
