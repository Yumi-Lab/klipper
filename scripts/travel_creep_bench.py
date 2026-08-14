#!/usr/bin/env python3
# YUMI: standalone correctness bench for the travel-creep gcode_move
# transform (PrinterExtruder.move/get_position, klippy/kinematics/extruder.py).
# Pure Python logic, no C involved -- exercises the REAL methods (bound onto
# a minimal fake object via types.MethodType), not a reimplementation, so a
# transcription bug here can't hide a real one. Exit 0 = all assertions held.
import math, os, sys, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                '..', 'klippy', 'kinematics'))
import extruder  # noqa: E402

FAILS = []
TOTAL = [0]


def check(label, cond, detail=''):
    TOTAL[0] += 1
    if not cond:
        FAILS.append('%s%s' % (label, ' -- ' + detail if detail else ''))
    print('%s %s%s' % ('OK  ' if cond else 'FAIL', label,
                       ' (%s)' % detail if detail and not cond else ''))


class FakeStepper:
    def __init__(self, rate=0., cap=1., min_dist=10.):
        self.travel_creep_rate = rate
        self.travel_creep_max = cap
        self.travel_creep_min_dist = min_dist
        self._creep_owed = 0.


class FakeChain:
    # YUMI: stands in for whatever move_with_transform was chained to us
    # (toolhead directly, or bed_mesh/skew_correction wrapping it) -- proves
    # our transform calls the NEXT link rather than swallowing the move.
    def __init__(self, start):
        self.pos = list(start)
        self.calls = []

    def get_position(self):
        return list(self.pos)

    def move(self, newpos, speed):
        self.calls.append((list(newpos), speed))
        self.pos = list(newpos)


def make_extruder(stepper):
    fake = types.SimpleNamespace()
    fake._backlash_steppers = lambda: [stepper]
    fake._creep_old_transform = FakeChain([0., 0., 0., 0.])
    fake.move = types.MethodType(extruder.PrinterExtruder.move, fake)
    fake.get_position = types.MethodType(
        extruder.PrinterExtruder.get_position, fake)
    return fake


# --- Case A: short travel (< min_dist) -- untouched ------------------------
es = FakeStepper(rate=0.002, cap=1., min_dist=10.)
pe = make_extruder(es)
pe.move([5., 0., 0., 0.], 3000.)  # 5mm travel, E unchanged
check('A short travel: no creep injected', es._creep_owed == 0.,
      'owed=%.6f' % es._creep_owed)
check('A short travel: E untouched on the chain',
      pe._creep_old_transform.pos[3] == 0.)

# --- Case B: long travel (>= min_dist) -- creep injected --------------------
es = FakeStepper(rate=0.002, cap=1., min_dist=10.)
pe = make_extruder(es)
pe.move([50., 0., 0., 0.], 3000.)  # 50mm travel, rate 0.002 -> 0.1mm creep
expected = 0.002 * 50.
check('B long travel: owed == rate*dist',
      abs(es._creep_owed - expected) < 1e-9,
      'owed=%.6f expected=%.6f' % (es._creep_owed, expected))
check('B long travel: E on the chain went NEGATIVE by that amount',
      abs(pe._creep_old_transform.pos[3] - (-expected)) < 1e-9,
      'E=%.6f' % pe._creep_old_transform.pos[3])

# --- Case C: cumulative travels cap at travel_creep_max ---------------------
es = FakeStepper(rate=0.5, cap=1., min_dist=10.)
pe = make_extruder(es)
pe.move([50., 0., 0., 0.], 3000.)   # would want 25mm of creep, capped to 1.
check('C first long travel capped at max', abs(es._creep_owed - 1.) < 1e-9,
      'owed=%.6f' % es._creep_owed)
pos_after_first = pe._creep_old_transform.pos[3]
pe.move([100., 0., 0., 0.], 3000.)  # already at the cap: must inject NOTHING more
check('C second long travel: owed stays at the cap (no over-injection)',
      abs(es._creep_owed - 1.) < 1e-9, 'owed=%.6f' % es._creep_owed)
check('C second long travel: E did not move further on the chain',
      pe._creep_old_transform.pos[3] == pos_after_first,
      'E=%.6f vs %.6f' % (pe._creep_old_transform.pos[3], pos_after_first))

# --- Case D: real extrusion resuming repays IN FULL -------------------------
es = FakeStepper(rate=0.002, cap=1., min_dist=10.)
pe = make_extruder(es)
pe.move([50., 0., 0., 0.], 3000.)          # owed = 0.1
owed_before = es._creep_owed
e_before = pe._creep_old_transform.pos[3]
pe.move([80., 0., 30., 2.], 1500.)          # real extrusion: E target = 2 (abs)
check('D repayment: owed cleared to zero', es._creep_owed == 0.,
      'owed=%.6f' % es._creep_owed)
expected_e = e_before + owed_before + (2. - e_before)  # de_gcode + repay
# Simpler equivalent check: the E actually SENT equals what the gcode asked
# for (2.) PLUS the owed amount -- net material stays exact, nothing lost.
check('D repayment: E sent == gcode target + repay, not just gcode target',
      abs(pe._creep_old_transform.pos[3] - (2. + owed_before)) < 1e-9,
      'E=%.6f expected=%.6f' % (pe._creep_old_transform.pos[3],
                                2. + owed_before))

# --- Case E: a real retract (de < 0) is untouched by this layer -------------
es = FakeStepper(rate=0.002, cap=1., min_dist=10.)
pe = make_extruder(es)
pe._creep_old_transform.pos = [0., 0., 0., 0.]
pe.move([0., 0., 0., -1.], 6000.)  # pure retract, no XY -- take-up's territory
check('E pure retract: creep layer leaves E exactly as the gcode asked',
      pe._creep_old_transform.pos[3] == -1.,
      'E=%.6f' % pe._creep_old_transform.pos[3])
check('E pure retract: nothing owed (never injected, nothing to repay)',
      es._creep_owed == 0.)

# --- Case G: three travels accumulate BEFORE hitting the cap ---------------
es = FakeStepper(rate=0.01, cap=1., min_dist=10.)
pe = make_extruder(es)
pe.move([20., 0., 0., 0.], 3000.)   # dist 20 -> +0.2
pe.move([50., 0., 0., 0.], 3000.)   # dist 30 -> +0.3 (cumulative 0.5)
pe.move([50., 25., 0., 0.], 3000.)  # dist 25 -> +0.25 (cumulative 0.75)
check('G three travels: owed sums exactly (no cap hit yet)',
      abs(es._creep_owed - 0.75) < 1e-9, 'owed=%.6f' % es._creep_owed)
check('G three travels: chain E == -owed, gcode frame stayed at 0 throughout',
      abs(pe._creep_old_transform.pos[3] - (-0.75)) < 1e-9,
      'E=%.6f' % pe._creep_old_transform.pos[3])
check('G get_position() reports 0 (gcode frame), not the raw chain E',
      abs(pe.get_position()[3] - 0.) < 1e-9,
      'reported=%.6f' % pe.get_position()[3])
pe.move([50., 25., 0., 3.], 1500.)  # now a real extrude, absolute E target 3
check('G repayment after accumulation: full owed added on top of the target',
      abs(pe._creep_old_transform.pos[3] - (3. + 0.75)) < 1e-9,
      'E=%.6f expected=%.6f' % (pe._creep_old_transform.pos[3], 3.75))
check('G owed cleared after repayment', es._creep_owed == 0.)

# --- Case F: feature OFF (rate=0, default) -- byte-for-byte untouched ------
es = FakeStepper(rate=0., cap=1., min_dist=10.)
pe = make_extruder(es)
pe.move([200., 0., 0., 0.], 3000.)
check('F feature off: long travel still passes through untouched',
      pe._creep_old_transform.pos[3] == 0.)

print()
if FAILS:
    print('FAILED (%d/%d):' % (len(FAILS), TOTAL[0]))
    for f in FAILS:
        print('  - %s' % f)
    sys.exit(1)
print('ALL OK (%d checks)' % TOTAL[0])
sys.exit(0)
