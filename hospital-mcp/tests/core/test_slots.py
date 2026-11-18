import threading
import unittest
from datetime import datetime

from hospital_mcp.errors import Codes, HospitalError
from hospital_mcp.services.slots import acquire, release
from tests.core.fakes import MemoryLockStore

T = datetime(2026, 11, 17, 10, 0)


def at(h, m):
    return datetime(2026, 11, 17, h, m)


class SlotLockTests(unittest.TestCase):
    def setUp(self):
        self.s = MemoryLockStore()

    def test_overlap_with_different_start_is_refused(self):
        """The case the unique (doctor_id, slot_start) index misses: 10:00-10:30 vs 10:15-10:30."""
        acquire(self.s, "A1:0", "D001", "MRN-1", at(10, 0), at(10, 30))
        with self.assertRaises(HospitalError) as cm:
            acquire(self.s, "A2:0", "D001", "MRN-2", at(10, 15), at(10, 30))
        self.assertEqual(cm.exception.code, Codes.SLOT_UNAVAILABLE)

    def test_failed_acquire_leaves_nothing_behind(self):
        acquire(self.s, "A1:0", "D001", "MRN-1", at(10, 15), at(10, 30))
        before = dict(self.s.d)
        with self.assertRaises(HospitalError):
            acquire(self.s, "A2:0", "D001", "MRN-2", at(10, 0), at(10, 30))  # 10:00 free, 10:15 taken
        self.assertEqual(self.s.d, before)

    def test_adjacent_slots_are_fine(self):
        acquire(self.s, "A1:0", "D001", "MRN-1", at(10, 0), at(10, 30))
        acquire(self.s, "A2:0", "D001", "MRN-2", at(10, 30), at(10, 45))

    def test_same_patient_cannot_be_in_two_places(self):
        acquire(self.s, "A1:0", "D001", "MRN-1", at(10, 0), at(10, 30))
        with self.assertRaises(HospitalError) as cm:
            acquire(self.s, "A2:0", "D002", "MRN-1", at(10, 0), at(10, 15))
        self.assertIn("patient", cm.exception.message)

    def test_release_frees_the_slot(self):
        acquire(self.s, "A1:0", "D001", "MRN-1", at(10, 0), at(10, 30))
        release(self.s, "A1:0")
        acquire(self.s, "A2:0", "D001", "MRN-2", at(10, 0), at(10, 30))

    def test_reschedule_pattern_old_and_new_locks_coexist_then_release(self):
        acquire(self.s, "A1:0", "D001", "MRN-1", at(10, 0), at(10, 30))
        acquire(self.s, "A1:1", "D001", "MRN-1", at(11, 0), at(11, 30))  # new slot first
        release(self.s, "A1:0")                                           # then free the old one
        acquire(self.s, "A2:0", "D001", "MRN-2", at(10, 0), at(10, 30))

    def test_bad_times_rejected(self):
        for start, end in ((at(10, 7), at(10, 30)), (at(10, 30), at(10, 0)), (at(10, 0), at(10, 0))):
            with self.assertRaises(HospitalError) as cm:
                acquire(self.s, "X:0", "D001", "MRN-1", start, end)
            self.assertEqual(cm.exception.code, Codes.INVALID_INPUT)

    def test_fifty_racers_one_winner(self):
        wins, errors, start_gate = [], [], threading.Event()

        def racer(i):
            start_gate.wait()
            try:
                acquire(self.s, f"A{i}:0", "D001", f"MRN-{i}", T, at(10, 30))
                wins.append(i)
            except HospitalError as e:
                errors.append(e.code)

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(50)]
        [t.start() for t in threads]
        start_gate.set()
        [t.join() for t in threads]
        self.assertEqual(len(wins), 1)
        self.assertEqual(errors, [Codes.SLOT_UNAVAILABLE] * 49)
        self.assertEqual(len(self.s.d), 4)  # only the winner's 2 cells x (doctor + patient)


if __name__ == "__main__":
    unittest.main()
