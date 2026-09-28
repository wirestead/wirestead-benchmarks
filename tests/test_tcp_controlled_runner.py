import copy
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from compare_controlled_tcp import validate
class ControlledValidation(unittest.TestCase):
    def setUp(self):
        self.row=dict(transport="tcp",strategy="reliable",payload_size="1024",main_cpu="0",sender_cpu="1",client_cpu="2",server_cpu="3",accepted_messages="10",accepted_bytes="10240",received_bytes="10240",client_queued_bytes_final="0",client_pending_bytes_final="0",accounting_supported="1",outstanding_requests_final="0",elapsed_ns="3000001000",duration_ms="3000")
        self.log="\n".join(f"ROLE role={role} tid={100+i} cpu={i} verified=1" for i,role in enumerate(["bench-main","bench-sender","bench-client","bench-server"]))
        self.log+="\nCALLBACK_ROLE role=bench-client tid=102 cpu=2 verified=1\nCALLBACK_ROLE role=bench-server tid=103 cpu=3 verified=1"
    def check(self):validate(self.row,self.log,"reliable",1024,[0,1,2,3])
    def test_valid(self):self.check()
    def test_rejects_bad_evidence(self):
        for field,value in [("received_bytes","9216"),("outstanding_requests_final","1"),("client_cpu","1"),("client_pending_bytes_final","1024"),("elapsed_ns","1"),("accepted_messages","0")]:
            with self.subTest(field=field):
                old=self.row[field];self.row[field]=value
                with self.assertRaises(ValueError):self.check()
                self.row[field]=old
    def test_missing_role(self):
        self.log=self.log.replace("role=bench-sender","role=unknown")
        with self.assertRaises(ValueError):self.check()
    def test_duplicate_tid(self):
        self.log=self.log.replace("tid=101","tid=100")
        with self.assertRaises(ValueError):self.check()
    def test_absent_ledger_is_explicit(self):
        self.row.update(accounting_supported="0",outstanding_requests_final="")
        self.check()

    def test_callback_role_mismatch(self):
        self.log=self.log.replace("CALLBACK_ROLE role=bench-client tid=102", "CALLBACK_ROLE role=bench-client tid=999")
        with self.assertRaises(ValueError):self.check()
