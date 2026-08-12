# Code for handling printer nozzle extruders
#
# Copyright (C) 2016-2025  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging
import stepper, chelper

# YUMI: bounds of the bowden backlash take-up, written once and reused by the
# config reader and the gcode command so they cannot drift apart.
BACKLASH_COEF_MAX = 5.
# Hard ceiling on the anticipation window. The smoothing already asks Klipper for
# up to 100 ms of forward moves and that is proven safe; going far beyond pushes
# step generation out of its sliding window ("Invalid sequence", see
# YUMI_PATCHES.md). A take-up ramp is a few ms, so this is never reached in
# practice -- it exists so a bad setting is REFUSED instead of crashing a print.
BACKLASH_RAMP_MAX = .100
# Profil en S f(u)=u^2(3-2u) sur une distance D en une duree T :
#   vitesse de pointe      v = 1,5 D / T          (au milieu)
#   acceleration de pointe a = 6 D / T^2          (aux deux bords)
# On en tire la duree minimale que chaque limite impose, et on garde la plus
# longue : la rampe respecte alors les DEUX, sans qu'aucune ne soit une moyenne.
SMOOTH_PEAK_RATIO = 1.5
SMOOTH_ACCEL_RATIO = 6.
BACKLASH_ACCEL_MAX = 100000.
DEFAULT_FILAMENT_D = 1.75
DEFAULT_BOWDEN_ID = 2.0
# Tightest radius a PTFE bowden is bent to on these machines, mm. Used only to
# cap the declared routing: a short tube cannot physically hold a full turn.
BOWDEN_BEND_RADIUS = 25.

class ExtruderStepper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.pressure_advance = self.pressure_advance_smooth_time = 0.
        # YUMI: smooth_time actually loaded in the kinematics (may be active
        # because of lead_time even when pressure_advance == 0).
        self._applied_smooth = 0.
        self.config_pa = config.getfloat('pressure_advance', 0., minval=0.)
        self.config_smooth_time = config.getfloat(
                'pressure_advance_smooth_time', 0.040, above=0., maxval=.200)
        # YUMI: bowden backlash take-up. The operator declares GEOMETRY, never
        # a play: the tube length and its bore. Everything else is deduced --
        # play = (bore - filament) * curvature -- so there is one number to
        # measure and no number to guess. bowden_length 0 (the default, i.e. not
        # declared) leaves the layer off and the stock path untouched.
        self.bowden_length = config.getfloat('bowden_length', 0., minval=0.)
        self.bowden_id = config.getfloat('bowden_id', DEFAULT_BOWDEN_ID,
                                         above=0.)
        # Routing hypothesis, in turns of tube. It is an assumption, not a
        # measurement: it is declared here so it can be seen and changed, never
        # hidden in the formula. Capped by what the length can physically bend.
        self.bowden_turns = config.getfloat('bowden_turns', 1., minval=0.)
        # The filament diameter is already known to [extruder]; fall back for a
        # standalone [extruder_stepper], which has no such setting.
        self.filament_d = config.getfloat('filament_diameter',
                                          DEFAULT_FILAMENT_D, above=0.)
        self.backlash_speed = config.getfloat('backlash_speed', 120., above=0.)
        # Sans borne d'acceleration, la rampe partait a pleine vitesse d'un coup :
        # sur un extrudeur demultiplie cela veut dire des centaines de tours par
        # minute appliques sans transition -- le moteur claque et peut perdre des
        # pas en silence. Cette limite est CELLE DU RATTRAPAGE : les mouvements
        # planifies, eux, restent bornes par max_extrude_only_accel, qui ne
        # s'applique pas ici puisque la couche n'est pas un mouvement planifie.
        self.backlash_accel = config.getfloat('backlash_accel', 2000., above=0.,
                                              maxval=BACKLASH_ACCEL_MAX)
        # The experimental knob: multiplies the COMPUTED play. 2 doubles it, 0.5
        # halves it. Tuned live like flow, without ever touching the geometry.
        self.backlash_coef = config.getfloat('backlash_coef', 1., minval=0.,
                                             maxval=BACKLASH_COEF_MAX)
        # Setup stepper
        self.stepper = stepper.PrinterStepper(config)
        ffi_main, ffi_lib = chelper.get_ffi()
        self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(),
                                       ffi_lib.extruder_stepper_free)
        self.stepper.set_stepper_kinematics(self.sk_extruder)
        self.motion_queue = None
        # YUMI: where the take-up currently stands, mirrored from the C list so
        # the planner only stamps real changes.
        self._backlash_target = 0.
        # Register commands
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        gcode = self.printer.lookup_object('gcode')
        if self.name == 'extruder':
            gcode.register_mux_command("SET_PRESSURE_ADVANCE", "EXTRUDER", None,
                                       self.cmd_default_SET_PRESSURE_ADVANCE,
                                       desc=self.cmd_SET_PRESSURE_ADVANCE_help)
        gcode.register_mux_command("SET_PRESSURE_ADVANCE", "EXTRUDER",
                                   self.name, self.cmd_SET_PRESSURE_ADVANCE,
                                   desc=self.cmd_SET_PRESSURE_ADVANCE_help)
        gcode.register_mux_command("SET_EXTRUDER_ROTATION_DISTANCE", "EXTRUDER",
                                   self.name, self.cmd_SET_E_ROTATION_DISTANCE,
                                   desc=self.cmd_SET_E_ROTATION_DISTANCE_help)
        gcode.register_mux_command("SYNC_EXTRUDER_MOTION", "EXTRUDER",
                                   self.name, self.cmd_SYNC_EXTRUDER_MOTION,
                                   desc=self.cmd_SYNC_EXTRUDER_MOTION_help)
    def _handle_connect(self):
        self._set_pressure_advance(self.config_pa, self.config_smooth_time)
        self._apply_backlash()
    def get_status(self, eventtime):
        motion_queuing = self.printer.lookup_object('motion_queuing')
        lead_time = motion_queuing.get_trapq_lead(self.stepper.get_trapq())
        return {'pressure_advance': self.pressure_advance,
                'smooth_time': self.pressure_advance_smooth_time,
                'lead_time': lead_time,
                'bowden_length': self.bowden_length,
                'bowden_id': self.bowden_id,
                'bowden_turns': self.bowden_turns,
                'backlash_coef': self.backlash_coef,
                'backlash_speed': self.backlash_speed,
                'backlash_accel': self.backlash_accel,
                'backlash_play': self._backlash_play(),
                'motion_queue': self.motion_queue}
    def note_extrude_dir(self, print_time, direction):
        # YUMI: called by the planner for every move, which is the ONLY place
        # that knows the order of things. The kinematics cannot: after a pause
        # it can no longer see which way the filament last went, and looking
        # further back reads moves Klipper may already have freed.
        play = self._backlash_play()
        if play <= 0. or not direction:
            return
        target = -play if direction < 0. else 0.
        if target == self._backlash_target:
            return
        self._backlash_target = target
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.extruder_backlash_flip(self.sk_extruder, print_time, target)
    def _backlash_play(self):
        # YUMI: the dead travel, deduced -- never typed. In a bend the filament
        # rests on the outer wall going in and the inner one coming back, so the
        # lost travel is the diametral clearance times the total turned angle.
        # theta is capped by length / min_radius: a 50 mm tube cannot hold a
        # full turn, and pretending otherwise is how a 960 mm tube ended up
        # claiming six turns and ten millimetres of play.
        if self.bowden_length <= 0.:
            return 0.
        clearance = self.bowden_id - self.filament_d
        if clearance <= 0.:
            return 0.
        theta = self.bowden_turns * 2. * math.pi
        theta = min(theta, self.bowden_length / BOWDEN_BEND_RADIUS)
        return clearance * theta * self.backlash_coef
    def _backlash_ramp(self):
        # Time to walk the play at the declared speed. A couple of millimetres
        # at a hundred mm/s: a handful of milliseconds, which is what keeps this
        # layer well inside the window step generation can serve.
        play = self._backlash_play()
        if play <= 0.:
            return 0.
        # La duree qui satisfait les deux limites a la fois.
        t_vel = SMOOTH_PEAK_RATIO * play / self.backlash_speed
        t_acc = math.sqrt(SMOOTH_ACCEL_RATIO * play / self.backlash_accel)
        return max(t_vel, t_acc)
    def _apply_backlash(self, gcmd=None):
        play, ramp = self._backlash_play(), self._backlash_ramp()
        if ramp > BACKLASH_RAMP_MAX:
            # Refuse, loudly. Silently clipping is what turned a bad setting
            # into a mid-print "Invalid sequence" instead of an error message.
            msg = ("bowden take-up would need %.0f ms (play %.2f mm at %.0f"
                   " mm/s, %.0f mm/s2); the limit is %.0f ms. Raise"
                   " backlash_speed or backlash_accel, or lower backlash_coef."
                   % (ramp * 1000., play, self.backlash_speed,
                      self.backlash_accel, BACKLASH_RAMP_MAX * 1000.))
            if gcmd is not None:
                raise gcmd.error(msg)
            raise self.printer.config_error(msg)
        toolhead = self.printer.lookup_object("toolhead")
        # Changing the scan window needs a full kinematic flush, like smoothing.
        toolhead.flush_step_generation()
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.extruder_set_backlash(self.sk_extruder, play, ramp)
        self._backlash_target = 0.
        motion_queuing = self.printer.lookup_object('motion_queuing')
        motion_queuing.check_step_generation_scan_windows()
    def _cur_lead(self):
        mq = self.printer.lookup_object('motion_queuing')
        return mq.get_trapq_lead(self.stepper.get_trapq())
    def _set_lead_time(self, gcmd, lead_time):
        # YUMI: lead belongs to the motion queue (trapq); apply it to whatever
        # trapq this stepper is currently bound to. Synced feeders share it.
        strapq = self.stepper.get_trapq()
        if strapq is None:
            gcmd.respond_info("Extruder stepper '%s' not bound to a motion "
                              "queue; LEAD_TIME ignored" % (self.name,))
            return
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        motion_queuing = self.printer.lookup_object('motion_queuing')
        motion_queuing.set_trapq_lead(strapq, lead_time)
        # YUMI: a lead can enable (or disable) smoothing even when PA==0, so the
        # smooth_time works WITH the lead without needing a fake tiny PA.
        new_smooth = (self.pressure_advance_smooth_time
                      if (self.pressure_advance or lead_time) else 0.)
        if new_smooth != self._applied_smooth:
            ffi_main, ffi_lib = chelper.get_ffi()
            ffi_lib.extruder_set_pressure_advance(
                    self.sk_extruder, 0., self.pressure_advance, new_smooth)
            self._applied_smooth = new_smooth
        motion_queuing.check_step_generation_scan_windows()
    def find_past_position(self, print_time):
        mcu_pos = self.stepper.get_past_mcu_position(print_time)
        return self.stepper.mcu_to_commanded_position(mcu_pos)
    def sync_to_extruder(self, extruder_name):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        motion_queuing = self.printer.lookup_object('motion_queuing')
        if not extruder_name:
            self.stepper.set_trapq(None)
            self.motion_queue = None
            motion_queuing.check_step_generation_scan_windows()
            self.printer.send_event("extruder_stepper:sync", self)
            return
        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None or not isinstance(extruder, PrinterExtruder):
            raise self.printer.command_error("'%s' is not a valid extruder."
                                             % (extruder_name,))
        self.stepper.set_position([extruder.last_position, 0., 0.])
        self.stepper.set_trapq(extruder.get_trapq())
        self.motion_queue = extruder_name
        motion_queuing.check_step_generation_scan_windows()
        # YUMI: la liste des steppers lies a un extrudeur change ici, et nulle
        # part ailleurs. On previent, plutot que de la reconstruire a chaque
        # mouvement.
        self.printer.send_event("extruder_stepper:sync", self)
    def _set_pressure_advance(self, pressure_advance, smooth_time):
        # YUMI: smoothing is active when PA>0 OR a lead is set on this trapq, so
        # smooth_time works at PA=0 alongside lead_time (no fake tiny PA needed).
        lead = self._cur_lead()
        new_smooth_time = smooth_time if (pressure_advance or lead) else 0.
        toolhead = self.printer.lookup_object("toolhead")
        ffi_main, ffi_lib = chelper.get_ffi()
        espa = ffi_lib.extruder_set_pressure_advance
        if new_smooth_time != self._applied_smooth:
            # Need full kinematic flush to change the smooth time
            toolhead.flush_step_generation()
            espa(self.sk_extruder, 0., pressure_advance, new_smooth_time)
            self._applied_smooth = new_smooth_time
            motion_queuing = self.printer.lookup_object('motion_queuing')
            motion_queuing.check_step_generation_scan_windows()
        else:
            toolhead.register_lookahead_callback(
                lambda print_time: espa(self.sk_extruder, print_time,
                                        pressure_advance, new_smooth_time))
        self.pressure_advance = pressure_advance
        self.pressure_advance_smooth_time = smooth_time
    cmd_SET_PRESSURE_ADVANCE_help = "Set pressure advance parameters"
    def cmd_default_SET_PRESSURE_ADVANCE(self, gcmd):
        extruder = self.printer.lookup_object('toolhead').get_extruder()
        if extruder.extruder_stepper is None:
            raise gcmd.error("Active extruder does not have a stepper")
        strapq = extruder.extruder_stepper.stepper.get_trapq()
        if strapq is not extruder.get_trapq():
            raise gcmd.error("Unable to infer active extruder stepper")
        extruder.extruder_stepper.cmd_SET_PRESSURE_ADVANCE(gcmd)
    def cmd_SET_PRESSURE_ADVANCE(self, gcmd):
        pressure_advance = gcmd.get_float('ADVANCE', self.pressure_advance,
                                          minval=0.)
        smooth_time = gcmd.get_float('SMOOTH_TIME',
                                     self.pressure_advance_smooth_time,
                                     minval=0., maxval=.200)
        self._set_pressure_advance(pressure_advance, smooth_time)
        lead_time = gcmd.get_float('LEAD_TIME', None, minval=0., maxval=0.5)
        if lead_time is not None:
            self._set_lead_time(gcmd, lead_time)
        # YUMI: bowden take-up. BOWDEN_LENGTH is the geometry (normally set once
        # in the config); BACKLASH_COEF is the experimental knob, meant to be
        # swept live during a calibration print exactly like flow.
        b_len = gcmd.get_float('BOWDEN_LENGTH', self.bowden_length, minval=0.)
        b_coef = gcmd.get_float('BACKLASH_COEF', self.backlash_coef, minval=0.,
                                maxval=BACKLASH_COEF_MAX)
        b_speed = gcmd.get_float('BACKLASH_SPEED', self.backlash_speed,
                                 above=0.)
        # The two settings that actually MOVE the play: the bore sets the
        # clearance, the routing sets the angle. Length only caps the angle, so
        # it stops mattering past a couple of hundred mm -- exposing these live
        # is what makes a sweep informative.
        b_id = gcmd.get_float('BOWDEN_ID', self.bowden_id, above=0.)
        b_turns = gcmd.get_float('BOWDEN_TURNS', self.bowden_turns, minval=0.)
        b_acc = gcmd.get_float('BACKLASH_ACCEL', self.backlash_accel, above=0.,
                               maxval=BACKLASH_ACCEL_MAX)
        cur = (self.bowden_length, self.backlash_coef, self.backlash_speed,
               self.bowden_id, self.bowden_turns, self.backlash_accel)
        if (b_len, b_coef, b_speed, b_id, b_turns, b_acc) != cur:
            keep = cur
            self.bowden_length, self.backlash_coef = b_len, b_coef
            self.backlash_speed = b_speed
            self.bowden_id, self.bowden_turns = b_id, b_turns
            self.backlash_accel = b_acc
            try:
                self._apply_backlash(gcmd)
            except Exception:
                # A refused setting must leave the machine exactly as it was.
                (self.bowden_length, self.backlash_coef, self.backlash_speed,
                 self.bowden_id, self.bowden_turns, self.backlash_accel) = keep
                raise
        motion_queuing = self.printer.lookup_object('motion_queuing')
        cur_lead = motion_queuing.get_trapq_lead(self.stepper.get_trapq())
        # YUMI: les noms affiches sont EXACTEMENT ceux de la commande, en
        # majuscules, pour se copier-coller sans traduction. Les valeurs
        # deduites gardent la minuscule : il n'existe pas de parametre pour
        # elles, les taper ne marcherait pas. L'API get_status, elle, garde les
        # noms Klipper d'origine -- c'est elle que lisent Mainsail et Moonraker.
        msg = ("ADVANCE: %.6f (configurable)\n"
               "SMOOTH_TIME: %.6f (configurable)\n"
               "LEAD_TIME: %.6f (configurable)\n"
               "BOWDEN_LENGTH: %.1f mm (configurable)\n"
               "BOWDEN_ID: %.2f mm (configurable)\n"
               "BOWDEN_TURNS: %.2f (configurable)\n"
               "BACKLASH_COEF: %.3f (configurable)\n"
               "BACKLASH_SPEED: %.1f mm/s (configurable)\n"
               "BACKLASH_ACCEL: %.0f mm/s2 (configurable)\n"
               "backlash_play: %.3f mm (deduced)\n"
               "backlash_ramp: %.1f ms (deduced)"
               % (pressure_advance, smooth_time, cur_lead, self.bowden_length,
                  self.bowden_id, self.bowden_turns, self.backlash_coef,
                  self.backlash_speed, self.backlash_accel,
                  self._backlash_play(), self._backlash_ramp() * 1000.))
        self.printer.set_rollover_info(self.name, "%s: %s" % (self.name, msg))
        gcmd.respond_info(msg, log=False)
    cmd_SET_E_ROTATION_DISTANCE_help = "Set extruder rotation distance"
    def cmd_SET_E_ROTATION_DISTANCE(self, gcmd):
        rotation_dist = gcmd.get_float('DISTANCE', None)
        if rotation_dist is not None:
            if not rotation_dist:
                raise gcmd.error("Rotation distance can not be zero")
            invert_dir, orig_invert_dir = self.stepper.get_dir_inverted()
            next_invert_dir = orig_invert_dir
            if rotation_dist < 0.:
                next_invert_dir = not orig_invert_dir
                rotation_dist = -rotation_dist
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.flush_step_generation()
            self.stepper.set_rotation_distance(rotation_dist)
            self.stepper.set_dir_inverted(next_invert_dir)
        else:
            rotation_dist, spr = self.stepper.get_rotation_distance()
        invert_dir, orig_invert_dir = self.stepper.get_dir_inverted()
        if invert_dir != orig_invert_dir:
            rotation_dist = -rotation_dist
        gcmd.respond_info("Extruder '%s' rotation distance set to %0.6f"
                          % (self.name, rotation_dist))
    cmd_SYNC_EXTRUDER_MOTION_help = "Set extruder stepper motion queue"
    def cmd_SYNC_EXTRUDER_MOTION(self, gcmd):
        ename = gcmd.get('MOTION_QUEUE')
        self.sync_to_extruder(ename)
        gcmd.respond_info("Extruder '%s' now syncing with '%s'"
                          % (self.name, ename))

# Tracking for hotend heater, extrusion motion queue, and extruder stepper
class PrinterExtruder:
    def __init__(self, config, extruder_num):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.last_position = 0.
        # Setup hotend heater
        pheaters = self.printer.load_object(config, 'heaters')
        gcode_id = 'T%d' % (extruder_num,)
        self.heater = pheaters.setup_heater(config, gcode_id)
        # Setup kinematic checks
        self.nozzle_diameter = config.getfloat('nozzle_diameter', above=0.)
        filament_diameter = config.getfloat(
            'filament_diameter', minval=self.nozzle_diameter)
        self.filament_area = math.pi * (filament_diameter * .5)**2
        def_max_cross_section = 4. * self.nozzle_diameter**2
        def_max_extrude_ratio = def_max_cross_section / self.filament_area
        max_cross_section = config.getfloat(
            'max_extrude_cross_section', def_max_cross_section, above=0.)
        self.max_extrude_ratio = max_cross_section / self.filament_area
        logging.info("Extruder max_extrude_ratio=%.6f", self.max_extrude_ratio)
        toolhead = self.printer.lookup_object('toolhead')
        max_velocity, max_accel = toolhead.get_max_velocity()
        self.max_e_velocity = config.getfloat(
            'max_extrude_only_velocity', max_velocity * def_max_extrude_ratio
            , above=0.)
        self.max_e_accel = config.getfloat(
            'max_extrude_only_accel', max_accel * def_max_extrude_ratio
            , above=0.)
        self.max_e_dist = config.getfloat(
            'max_extrude_only_distance', 50., minval=0.)
        self.instant_corner_v = config.getfloat(
            'instantaneous_corner_velocity', 1., minval=0.)
        # Setup extruder trapq (trapezoidal motion queue)
        self.motion_queuing = self.printer.load_object(config, 'motion_queuing')
        self.trapq = self.motion_queuing.allocate_trapq()
        self.trapq_append = self.motion_queuing.lookup_trapq_append()
        # YUMI: extruder lead time (pure anticipation / coast). The lead shifts
        # the trapq content earlier in time at planner level (see process_move);
        # it is NOT a kin_extruder.c / calc_position change. Cache the trapq
        # address so process_move reads the live lead with one dict lookup.
        self._trapq_addr = self.motion_queuing.trapq_addr(self.trapq)
        config_lead = config.getfloat('lead_time', 0., minval=0., maxval=0.5)
        if config_lead:
            self.motion_queuing.set_trapq_lead(self.trapq, config_lead)
            # Refresh step-generation scan windows once syncemitters exist
            self.printer.register_event_handler(
                "klippy:connect",
                lambda: self.motion_queuing.check_step_generation_scan_windows())
        # Setup extruder stepper
        # YUMI: cache des steppers concernes par le rattrapage de jeu
        self._bl_steppers = None
        self.printer.register_event_handler("extruder_stepper:sync",
                                            self._handle_stepper_sync)
        self.extruder_stepper = None
        if (config.get('step_pin', None) is not None
            or config.get('dir_pin', None) is not None
            or config.get('rotation_distance', None) is not None):
            self.extruder_stepper = ExtruderStepper(config)
            self.extruder_stepper.stepper.set_trapq(self.trapq)
        # Register commands
        gcode = self.printer.lookup_object('gcode')
        if self.name == 'extruder':
            toolhead.set_extruder(self, 0.)
            gcode.register_command("M104", self.cmd_M104)
            gcode.register_command("M109", self.cmd_M109)
        gcode.register_mux_command("ACTIVATE_EXTRUDER", "EXTRUDER",
                                   self.name, self.cmd_ACTIVATE_EXTRUDER,
                                   desc=self.cmd_ACTIVATE_EXTRUDER_help)
    def get_status(self, eventtime):
        sts = self.heater.get_status(eventtime)
        sts['can_extrude'] = self.heater.can_extrude
        if self.extruder_stepper is not None:
            sts.update(self.extruder_stepper.get_status(eventtime))
        return sts
    def get_name(self):
        return self.name
    def get_heater(self):
        return self.heater
    def get_trapq(self):
        return self.trapq
    def get_axis_gcode_id(self):
        return 'E'
    def stats(self, eventtime):
        return self.heater.stats(eventtime)
    def check_move(self, move, ea_index):
        if not self.heater.can_extrude:
            raise self.printer.command_error(
                "Extrude below minimum temp\n"
                "See the 'min_extrude_temp' config option for details")
        axis_r = move.axes_r[ea_index]
        axis_d = move.axes_d[ea_index]
        if (not move.axes_d[0] and not move.axes_d[1]) or axis_r < 0.:
            # Extrude only move (or retraction move) - limit accel and velocity
            if abs(axis_d) > self.max_e_dist:
                raise self.printer.command_error(
                    "Extrude only move too long (%.3fmm vs %.3fmm)\n"
                    "See the 'max_extrude_only_distance' config"
                    " option for details" % (axis_d, self.max_e_dist))
            inv_extrude_r = 1. / abs(axis_r)
            move.limit_speed(self.max_e_velocity * inv_extrude_r,
                             self.max_e_accel * inv_extrude_r)
        elif axis_r > self.max_extrude_ratio:
            if axis_d <= self.nozzle_diameter * self.max_extrude_ratio:
                # Permit extrusion if amount extruded is tiny
                return
            area = axis_r * self.filament_area
            logging.debug("Overextrude: %s vs %s (area=%.3f dist=%.3f)",
                          axis_r, self.max_extrude_ratio, area, move.move_d)
            raise self.printer.command_error(
                "Move exceeds maximum extrusion (%.3fmm^2 vs %.3fmm^2)\n"
                "See the 'max_extrude_cross_section' config option for details"
                % (area, self.max_extrude_ratio * self.filament_area))
    def calc_junction(self, prev_move, move, ea_index):
        diff_r = move.axes_r[ea_index] - prev_move.axes_r[ea_index]
        if diff_r:
            return (self.instant_corner_v / abs(diff_r))**2
        return move.max_cruise_v2
    def _handle_stepper_sync(self, stepper):
        # YUMI: invalide le cache ; il se reconstruit au prochain mouvement.
        self._bl_steppers = None
    def _backlash_steppers(self):
        # YUMI: tous les extruder_stepper actuellement lies a CETTE file, tete
        # comprise. Reconstruit seulement quand une synchro a change (evenement
        # extruder_stepper:sync), pas a chaque mouvement.
        if self._bl_steppers is None:
            out = []
            for _, obj in self.printer.lookup_objects('extruder_stepper'):
                # lookup_objects rend l'ENVELOPPE PrinterExtruderStepper, pas le
                # ExtruderStepper qu'elle contient. Lire motion_queue dessus
                # levait un AttributeError en plein flush et arretait Klipper.
                es = getattr(obj, 'extruder_stepper', obj)
                if getattr(es, 'motion_queue', None) == self.name:
                    out.append(es)
            # La tete elle-meme, si elle n'est pas deja passee par la boucle.
            if self.extruder_stepper is not None \
                    and self.extruder_stepper not in out:
                out.append(self.extruder_stepper)
            self._bl_steppers = out
        return self._bl_steppers
    def process_move(self, print_time, move, ea_index):
        axis_r = move.axes_r[ea_index]
        accel = move.accel * axis_r
        start_v = move.start_v * axis_r
        cruise_v = move.cruise_v * axis_r
        can_pressure_advance = False
        if axis_r > 0. and (move.axes_d[0] or move.axes_d[1]):
            can_pressure_advance = True
        # YUMI: shift the extruder trapq content earlier by the lead time so the
        # extruder finishes pushing `lead` before the toolhead stops (coast).
        # Positions are untouched; only the time base of the E trapq moves.
        lead = self.motion_queuing.trapq_leads.get(self._trapq_addr, 0.)
        # Queue movement (x is extruder movement, y is pressure advance flag)
        # YUMI: tell the take-up which way this move goes, BEFORE it is queued.
        # The layer then walks the gap so that it is closed exactly when this
        # move begins -- the start of the ramp is shifted, never the ramp itself.
        # ALL steppers pushing into this queue must be told, not just the head:
        # on a bowden it is the SYNCED FEEDER that fights the gap, and telling
        # only self.extruder_stepper left every feeder's take-up disarmed.
        if axis_r:
            for es in self._backlash_steppers():
                es.note_extrude_dir(print_time - lead, axis_r)
        self.trapq_append(self.trapq, print_time - lead,
                          move.accel_t, move.cruise_t, move.decel_t,
                          move.start_pos[ea_index], 0., 0.,
                          1., can_pressure_advance, 0.,
                          start_v, cruise_v, accel)
        self.last_position = move.end_pos[ea_index]
    def find_past_position(self, print_time):
        if self.extruder_stepper is None:
            return 0.
        return self.extruder_stepper.find_past_position(print_time)
    def cmd_M104(self, gcmd, wait=False):
        # Set Extruder Temperature
        temp = gcmd.get_float('S', 0.)
        index = gcmd.get_int('T', None, minval=0)
        if index is not None:
            section = 'extruder'
            if index:
                section = 'extruder%d' % (index,)
            extruder = self.printer.lookup_object(section, None)
            if extruder is None:
                if temp <= 0.:
                    return
                raise gcmd.error("Extruder not configured")
        else:
            extruder = self.printer.lookup_object('toolhead').get_extruder()
        pheaters = self.printer.lookup_object('heaters')
        pheaters.set_temperature(extruder.get_heater(), temp, wait)
    def cmd_M109(self, gcmd):
        # Set Extruder Temperature and Wait
        self.cmd_M104(gcmd, wait=True)
    cmd_ACTIVATE_EXTRUDER_help = "Change the active extruder"
    def cmd_ACTIVATE_EXTRUDER(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        if toolhead.get_extruder() is self:
            gcmd.respond_info("Extruder %s already active" % (self.name,))
            return
        gcmd.respond_info("Activating extruder %s" % (self.name,))
        toolhead.flush_step_generation()
        toolhead.set_extruder(self, self.last_position)
        self.printer.send_event("extruder:activate_extruder")

# Dummy extruder class used when a printer has no extruder at all
class DummyExtruder:
    def __init__(self, printer):
        self.printer = printer
    def check_move(self, move, ea_index):
        raise move.move_error("Extrude when no extruder present")
    def find_past_position(self, print_time):
        return 0.
    def calc_junction(self, prev_move, move, ea_index):
        return move.max_cruise_v2
    def get_name(self):
        return ""
    def get_heater(self):
        raise self.printer.command_error("Extruder not configured")
    def get_trapq(self):
        return None
    def get_axis_gcode_id(self):
        return 'E'

def add_printer_objects(config):
    printer = config.get_printer()
    for i in range(99):
        section = 'extruder'
        if i:
            section = 'extruder%d' % (i,)
        if not config.has_section(section):
            break
        pe = PrinterExtruder(config.getsection(section), i)
        printer.add_object(section, pe)
