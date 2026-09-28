#!/usr/bin/env python3
"""Opt-in integration checks: python3 tests/test_tcp_controlled.py /path/to/binary."""
import csv,os,pathlib,re,socket,subprocess,sys,tempfile,unittest
BINARY = pathlib.Path(sys.argv.pop(1)).resolve() if __name__ == "__main__" else None
@unittest.skipUnless(BINARY, "pass the controlled TCP binary to run integration checks")
class ControlledTCP(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.cpus=sorted(os.sched_getaffinity(0));self.cpus=(self.cpus*4)[:4]
  with socket.socket() as s:s.bind(('127.0.0.1',0));self.port=s.getsockname()[1]
  self.output=pathlib.Path(self.tmp.name)/'result.csv'
 def command(self,strategy):
  return [str(BINARY),'--strategy',strategy,'--main-cpu',str(self.cpus[0]),'--sender-cpu',str(self.cpus[1]),'--client-cpu',str(self.cpus[2]),'--server-cpu',str(self.cpus[3]),'--port',str(self.port),'--duration-ms','200','--warmup-messages','16','--payload-size','1024','--csv-output',str(self.output)]
 def test_strategies_and_roles(self):
  for strategy in ['reliable','besteffort']:
   with self.subTest(strategy=strategy):
    p=subprocess.run(self.command(strategy),capture_output=True,text=True,timeout=20)
    self.assertEqual(p.returncode,0,p.stdout+p.stderr)
    roles=re.findall(r'^ROLE role=(\S+) tid=(\d+) cpu=(\d+) verified=1',p.stdout,re.MULTILINE)
    self.assertEqual({name:int(cpu) for name,tid,cpu in roles},dict(zip(['bench-main','bench-sender','bench-client','bench-server'],self.cpus)))
    self.assertEqual(len({tid for name,tid,cpu in roles}),4)
    callbacks=re.findall(r'^CALLBACK_ROLE role=(\S+) tid=(\d+) cpu=(\d+) verified=1',p.stdout,re.MULTILINE)
    self.assertEqual(set(callbacks),{x for x in roles if x[0] in ['bench-client','bench-server']})
    with self.output.open() as f:rows=list(csv.DictReader(f))
    self.assertEqual(len(rows),1);row=rows[0];self.assertEqual(row['strategy'],strategy)
    self.assertGreater(int(row['accepted_messages']),0)
    self.assertEqual(int(row['accepted_messages'])*1024,int(row['accepted_bytes']))
    self.assertEqual(row['accepted_bytes'],row['received_bytes'])
    self.assertGreaterEqual(int(row['elapsed_ns']),200000000)
    self.assertEqual(int(row['client_queued_bytes_final']),0);self.assertEqual(int(row['client_pending_bytes_final']),0)
 def test_invalid_options(self):
  for extra in [['--strategy','unknown'],['--sender-cpu','-1'],['--client-cpu','1024'],['--warmup-messages','0'],['--payload-size','1x'],['--duration-ms']]:
   with self.subTest(extra=extra):
    p=subprocess.run(self.command('reliable')+extra,capture_output=True,text=True,timeout=5)
    self.assertNotEqual(p.returncode,0);self.assertFalse(self.output.exists())
 def test_worker_pin_failure_unwinds(self):
  if os.cpu_count()>=1024:self.skipTest('no portable offline CPU')
  p=subprocess.run(self.command('reliable')+['--client-cpu','1023'],capture_output=True,text=True,timeout=5)
  self.assertNotEqual(p.returncode,0);self.assertIn('cannot pin bench-client',p.stderr)
 def test_server_start_failure_unwinds(self):
  with socket.socket() as blocker:
   blocker.bind(('127.0.0.1',self.port));blocker.listen()
   p=subprocess.run(self.command('reliable'),capture_output=True,text=True,timeout=20)
   self.assertNotEqual(p.returncode,0);self.assertFalse(self.output.exists())
if __name__=='__main__':unittest.main()
