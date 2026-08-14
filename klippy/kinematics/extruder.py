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
# La resorption se fait en un nombre FIXE de paliers, jamais en continu. Un jalon
# est un evenement : a 450 par seconde ils se chevauchent tous (chacun ouvre une
# interpolation de plusieurs dizaines de ms), l'offset tremble au lieu de ramper,
# et stepcompress finit par refuser la sequence. Huit paliers suffisent : l'oeil
# ne distingue pas un escalier de 8 marches d'une pente sur 20 mm de cordon.
BACKLASH_BLEED_STEPS = 8
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
        # DEDUCTION AU RETOUR. A la rétraction on ouvre le jeu en ENTIER -- sinon
        # la depressurisation est avalee. Au retour on repousse ce jeu MOINS
        # `backlash_deduct` : la pression n'est donc pas encore la quand la buse
        # arrive, ce qui evite la goutte formee dans le vide juste avant.
        self.backlash_deduct = config.getfloat('backlash_deduct', 0., minval=0.)
        # La deduction ne peut PAS tenir seule : si le decalage se repose a
        # -deduct, la retraction suivante repart de la et ne transmet plus que
        # (jeu - deduct) -- les deux cotes se reduisent pareil, l'asymetrie
        # disparait. Pour que la RETRACTION reste complete et que seul le RETOUR
        # soit reduit, le residu doit revenir a zero entre les deux. On le rend
        # pendant l'extrusion continue, sur cette distance : c'est le seul moment
        # ou le jeu est ferme et tenu ferme par la pression.
        self.backlash_bleed = config.getfloat('backlash_bleed', 20., above=0.)
        self._bleed_left = 0.
        # EXTRA LENGTH ON RESTART, cote firmware. A la difference du rattrapage,
        # celui-ci DEPOSE de la matiere : il s'ajoute a chaque reprise et ne
        # revient jamais. Le decalage cesse donc d'etre borne -- c'est voulu,
        # c'est ce que fait deja le trancheur, sauf que lui l'ecrit dans le
        # G-code. Ici Klipper ne le compte pas comme de l'extrusion : a n'utiliser
        # que pour REGLER en direct, puis reporter la valeur dans le trancheur.
        self.backlash_restart = config.getfloat('backlash_restart', 0.,
                                                minval=0.)
        self._restart_base = 0.
        # The experimental knob: multiplies the COMPUTED play. 2 doubles it, 0.5
        # halves it. Tuned live like flow, without ever touching the geometry.
        self.backlash_coef = config.getfloat('backlash_coef', 1., minval=0.,
                                             maxval=BACKLASH_COEF_MAX)
        # YUMI: TRAVEL CREEP -- distinct from the backlash take-up above. The
        # take-up walks the bowden's dead travel ONCE at the reversal, then
        # HOLDS. Video evidence (Nicolas, 2026-08-14) : the nozzle stays clean
        # on short travels, but past ~1cm of empty move a drop keeps forming
        # PROGRESSIVELY during the travel itself -- residual melt-zone
        # pressure that keeps relaxing beyond what the one-shot take-up
        # covers. This layer keeps retracting a little for as long as a real
        # travel (no extrusion) continues, and repays it in FULL on the next
        # real extruding move -- disjoint from the take-up's own moves (which
        # always carry E), so the two never fight over the same move.
        # 0 = off (default) : the stock path is untouched.
        self.travel_creep_rate = config.getfloat('travel_creep_rate', 0.,
                                                  minval=0.)
        self.travel_creep_max = config.getfloat('travel_creep_max', 1.,
                                                 above=0.)
        self.travel_creep_min_dist = config.getfloat(
            'travel_creep_min_dist', 10., above=0.)
        # Mm actuellement injectes en trop, a rendre au prochain vrai mouvement
        # d'extrusion. Jamais persiste au-dela d'une session : au pire on perd
        # quelques diximes de mm de matiere a une pause/fin de print, jamais
        # une sur-extrusion.
        self._creep_owed = 0.
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
        self._backlash_flips = 0
        # print_time du dernier move EXTRUDANT vu par le planner : c'est lui
        # (plus/moins la fenetre de scan) qui porte les pas d'une rampe de
        # retour posee a la fin de la file. Sert a savoir si cette rampe a
        # un porteur, cf. _apply_backlash.
        self._last_extrude_time = 0.
        # Miroir des parametres DEJA ecrits cote C (dernier extruder_set_backlash
        # effectif) : la fenetre de scan active en C est max(hst, _c_ramp) tant
        # que _c_play > 0. Indispensable pour savoir, AVANT de poser un jalon,
        # si sa rampe tient dans la fenetre que la generation va appliquer.
        self._c_play = 0.
        self._c_ramp = 0.
        # Derniere rampe non nulle : sert a ramener l'offset a zero quand le jeu
        # tombe a zero, au lieu de couper net.
        self._last_ramp = .02
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
                'backlash_deduct': self.backlash_deduct,
                'backlash_bleed': self.backlash_bleed,
                'backlash_restart': self.backlash_restart,
                # Total ajoute depuis le demarrage : c'est de la matiere que
                # Klipper ne compte pas ailleurs, autant la rendre visible.
                'backlash_restart_total': self._restart_base,
                'backlash_play': self._backlash_play(),
                # Ce que le planner a REELLEMENT jalonne en dernier, et combien
                # de fois. Sans ca, on ne peut pas distinguer "le reglage est
                # pris" de "le reglage agit sur le moteur" -- les deux se
                # ressemblent dans l'API et seul le second compte.
                'backlash_target': self._backlash_target,
                'backlash_flips': self._backlash_flips,
                'travel_creep_rate': self.travel_creep_rate,
                'travel_creep_max': self.travel_creep_max,
                'travel_creep_min_dist': self.travel_creep_min_dist,
                'travel_creep_owed': self._creep_owed,
                'motion_queue': self.motion_queue}
    def note_extrude_dir(self, print_time, direction, dist=0.):
        # YUMI: called by the planner for every move, which is the ONLY place
        # that knows the order of things. The kinematics cannot: after a pause
        # it can no longer see which way the filament last went, and looking
        # further back reads moves Klipper may already have freed.
        if not direction:
            return
        self._last_extrude_time = print_time
        play = self._backlash_play()
        # A jeu nul on jalonne QUAND MEME, avec une cible de 0 : c'est ce jalon
        # qui ramene l'offset a zero par la rampe. Sortir ici laisserait le
        # decalage fige a sa derniere valeur, et il faudrait bien le rendre un
        # jour -- d'un coup, donc en cassant stepcompress.
        if play > 0. and direction < 0.:
            target = self._restart_base - play   # traction : jeu entier
            self._bleed_left = 0.
        elif play > 0. and self._bleed_left > 0.:
            # On rend le residu PROPORTIONNELLEMENT a l'extrusion parcourue : le
            # manque s'etale sur la ligne au lieu de revenir d'un bloc. Attendre
            # puis tout rendre d'un coup laissait un renflement de 0,1 mm en un
            # dixieme de seconde, quelque part au milieu du trait.
            self._bleed_left -= abs(dist)
            if self._bleed_left <= 0.:
                self._bleed_left = 0.
                target = self._restart_base
            else:
                # Fraction restante ARRONDIE au palier : la cible ne change donc
                # qu'au plus BACKLASH_BLEED_STEPS fois par resorption.
                reste = self._bleed_left / self.backlash_bleed
                pal = math.ceil(reste * BACKLASH_BLEED_STEPS)
                target = (self._restart_base
                          - min(self.backlash_deduct, play)
                            * pal / BACKLASH_BLEED_STEPS)
        elif play > 0. and self.backlash_deduct > 0. \
                and self._backlash_target == -play:
            # Retour : on repousse tout SAUF la deduction. Le decalage se REPOSE
            # donc a -deduct pendant l'impression au lieu de revenir a zero : la
            # pression arrive moins fort a la buse, ce qui evite la goutte formee
            # dans le vide juste avant l'arrivee. Nicolas compense au trancheur.
            # Pas de derive : les deux cibles sont ABSOLUES, le decalage alterne
            # entre deux points fixes, il ne s'accumule jamais.
            # Premiere poussee apres une traction : on repousse tout SAUF la
            # deduction, et on arme la resorption.
            # Reprise : c'est ICI qu'on ajoute l'extra restart, une seule fois,
            # au moment ou on repart vraiment extruder.
            self._restart_base += self.backlash_restart
            target = self._restart_base - min(self.backlash_deduct, play)
            self._bleed_left = self.backlash_bleed
        else:
            target = self._restart_base
        if target == self._backlash_target:
            return
        self._backlash_target = target
        self._backlash_flips += 1
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.extruder_backlash_flip(self.sk_extruder, print_time, target,
                                       self._backlash_ramp())
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
            # Jeu nul : la couche ne doit pas s'eteindre, elle doit REDESCENDRE.
            # On garde donc la derniere rampe connue, le temps que l'offset
            # revienne a zero proprement. Sans elle, la coupure serait un saut.
            return self._last_ramp
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
        ffi_main, ffi_lib = chelper.get_ffi()
        motion_queuing = self.printer.lookup_object('motion_queuing')
        # RAMENER LE DECALAGE A ZERO AVANT DE TOUCHER AUX PARAMETRES.
        # Six arrets machine sont partis d'ici : eteindre la couche, changer de
        # file, changer la rampe... a chaque fois un parametre bougeait pendant
        # que l'offset valait autre chose que zero, et la position sautait.
        # Corriger cas par cas n'a fait qu'ouvrir la porte suivante. On pose donc
        # le principe : la couche revient a zero PAR SA RAMPE, on attend que les
        # pas soient emis, et seulement ensuite on change ce qu'on veut.
        deferred = False
        if self._backlash_target:
            # MAIS la rampe de retour doit tenir dans la fenetre de scan ACTIVE
            # en C au moment ou elle est generee. La poser avec la NOUVELLE
            # rampe alors que la fenetre est encore l'ANCIENNE (plus etroite,
            # ex. COEF 1.0->1.5 : 88,4 ms contre 58,9 ms) laisse un TROU : au
            # dela de gen_steps_post_active apres le dernier move extrudant,
            # itersolve ne genere plus de pas pour ce stepper -- la queue de la
            # rampe, si elle court sur des moves SANS extrusion (travel, Z-hop),
            # n'est jamais emise. sk->commanded_pos reste alors decale du jeu
            # entier ; a la reprise, calc_position saute de cette valeur d'un
            # coup et stepcompress refuse la sequence ("Invalid sequence",
            # arret machine quelques secondes APRES la commande, crash live du
            # 2026-08-14 pendant une impression active). Si la nouvelle rampe
            # depasse la fenetre courante, on ELARGIT D'ABORD la fenetre (et on
            # resynchronise kin_flush_delay AVANT le moindre flush, cf. la
            # regle YUMI_PATCHES) ; poser les parametres deux fois est
            # idempotent. Jeu nul : la rampe de retour vaut _last_ramp, qui EST
            # la fenetre courante -- le cas ne se presente pas.
            hst = self.pressure_advance_smooth_time * .5
            c_win = hst
            if self._c_play > 0. and self._c_ramp > c_win:
                c_win = self._c_ramp
            if ramp > c_win:
                ffi_lib.extruder_set_backlash(self.sk_extruder, play, ramp)
                self._c_play, self._c_ramp = play, ramp
                motion_queuing.check_step_generation_scan_windows()
            t_r = toolhead.get_last_move_time()
            if play > 0. and self._last_extrude_time < t_r - 2. * ramp:
                # ...et il faut EN PLUS que la fenetre de la rampe contienne
                # un move EXTRUDANT pas encore genere : itersolve ne genere
                # ce stepper que sur ses propres moves (+/- la fenetre), donc
                # sans porteur la rampe n'est JAMAIS emise quand bien meme
                # elle tiendrait dans la fenetre. Queue vide ou drainee
                # (pause, M109, G4, fin de print...) : la rampe posee a t_r
                # court dans le vide, commanded_pos reste decale du jeu, et
                # le reset ci-dessous perdrait la memoire de cet offset -> le
                # prochain move extrudant sauterait du jeu entier
                # ("Invalid sequence", repro backlash_drained_reconfig.test).
                # On ne pose alors RIEN : l'offset reste ou il est et le
                # prochain move extrudant le re-jalonne avec les NOUVEAUX
                # parametres -- continu par construction, l'invariant de
                # non-recouvrement impose a l'insertion le garantit (cf.
                # extruder_backlash_flip). Le reset est saute lui aussi.
                # Reserve au jeu NON NUL : a jeu nul la fenetre C retombe a
                # hst (extruder_update_scan_window ignore backlash_ramp quand
                # play==0) et l'offset en suspens ne pourrait plus etre
                # genere du tout -- le retour par la rampe historique reste
                # le bon chemin pour l'extinction.
                deferred = True
            else:
                ffi_lib.extruder_backlash_flip(self.sk_extruder, t_r, 0., ramp)
                self._backlash_target = 0.
                toolhead.dwell(ramp)
        toolhead.flush_step_generation()
        if not deferred:
            # Repartir d'un historique vierge : plus aucun jalon ne peut etre
            # relu avec les nouveaux reglages.
            ffi_lib.extruder_backlash_reset(self.sk_extruder)
            self._backlash_target = 0.
            self._restart_base = 0.
            self._bleed_left = 0.
            self._last_extrude_time = 0.
        ffi_lib.extruder_set_backlash(self.sk_extruder, play, ramp)
        self._c_play, self._c_ramp = play, ramp
        if ramp > 0.:
            self._last_ramp = ramp
        # extruder_set_backlash vient de reecrire gen_steps_pre/post_active sur
        # CE stepper (meme champ C que le smooth_time de la PA -- "one writer,
        # so they agree", cf. kin_extruder.c). _set_pressure_advance() prevoit
        # motion_queuing APRES avoir touche ce champ ; _apply_backlash() ne le
        # faisait jamais. kin_flush_delay restait donc sur l'ANCIENNE fenetre,
        # plus etroite -> l'historique trapq est purge trop tot -> stepcompress
        # relit du mouvement deja libere des qu'on elargit la rampe sur une
        # couche deja active (BACKLASH_SPEED 80->50 en direct : "Internal error
        # in stepcompress", 2026-08-13). Meme remede que la PA : re-synchroniser
        # kin_flush_delay avec la fenetre qu'on vient d'ecrire.
        motion_queuing.check_step_generation_scan_windows()
        # Le prochain mouvement re-jalonne la cible avec le NOUVEAU jeu : c'est
        # ce jalon qui fait redescendre (ou remonter) l'offset en douceur.
        # En mode differe (queue sans porteur), on GARDE la cible courante :
        # elle decrit l'offset physiquement en cours, et c'est le jalon pose
        # par le prochain move extrudant qui le ramenera -- en continu.
        if not deferred:
            self._backlash_target = None
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
        # YUMI: changer de file remet le stepper a une position qui IGNORE
        # l'offset de rattrapage. Un jalon survivant de l'attache precedente le
        # ferait donc reapparaitre d'un coup. On repart d'un historique vierge.
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.extruder_backlash_reset(self.sk_extruder)
        self._backlash_target = 0.
        self._restart_base = 0.
        self._bleed_left = 0.
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
        b_ded = gcmd.get_float('BACKLASH_DEDUCT', self.backlash_deduct,
                               minval=0.)
        b_bld = gcmd.get_float('BACKLASH_BLEED', self.backlash_bleed, above=0.)
        b_res = gcmd.get_float('BACKLASH_RESTART', self.backlash_restart,
                               minval=0.)
        # YUMI: travel creep -- independent of the take-up above (different
        # moves, different C-side mechanism entirely: this never touches
        # gen_steps_pre/post_active, it only edits E targets at the gcode_move
        # transform, cf. PrinterExtruder.move). No _apply_backlash() call
        # needed for these three -- just store them, the transform reads them
        # live on the next travel move.
        t_rate = gcmd.get_float('TRAVEL_CREEP_RATE', self.travel_creep_rate,
                                minval=0.)
        t_max = gcmd.get_float('TRAVEL_CREEP_MAX', self.travel_creep_max,
                               above=0.)
        t_min = gcmd.get_float('TRAVEL_CREEP_MIN_DIST',
                               self.travel_creep_min_dist, above=0.)
        self.travel_creep_rate, self.travel_creep_max = t_rate, t_max
        self.travel_creep_min_dist = t_min
        cur = (self.bowden_length, self.backlash_coef, self.backlash_speed,
               self.bowden_id, self.bowden_turns, self.backlash_accel,
               self.backlash_deduct, self.backlash_bleed,
               self.backlash_restart)
        if (b_len, b_coef, b_speed, b_id, b_turns, b_acc, b_ded, b_bld,
                b_res) != cur:
            keep = cur
            self.bowden_length, self.backlash_coef = b_len, b_coef
            self.backlash_speed = b_speed
            self.bowden_id, self.bowden_turns = b_id, b_turns
            self.backlash_accel = b_acc
            self.backlash_deduct, self.backlash_bleed = b_ded, b_bld
            self.backlash_restart = b_res
            try:
                self._apply_backlash(gcmd)
            except Exception:
                # A refused setting must leave the machine exactly as it was.
                (self.bowden_length, self.backlash_coef, self.backlash_speed,
                 self.bowden_id, self.bowden_turns, self.backlash_accel,
                 self.backlash_deduct, self.backlash_bleed,
                 self.backlash_restart) = keep
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
               "BACKLASH_DEDUCT: %.3f mm (configurable)\n"
               "BACKLASH_BLEED: %.1f mm (configurable)\n"
               "BACKLASH_RESTART: %.3f mm (configurable)\n"
               "TRAVEL_CREEP_RATE: %.4f mm/mm (configurable)\n"
               "TRAVEL_CREEP_MAX: %.3f mm (configurable)\n"
               "TRAVEL_CREEP_MIN_DIST: %.1f mm (configurable)\n"
               "backlash_play: %.3f mm (deduced)\n"
               "backlash_ramp: %.1f ms (deduced)"
               % (pressure_advance, smooth_time, cur_lead, self.bowden_length,
                  self.bowden_id, self.bowden_turns, self.backlash_coef,
                  self.backlash_speed, self.backlash_accel,
                  self.backlash_deduct, self.backlash_bleed,
                  self.backlash_restart, self.travel_creep_rate,
                  self.travel_creep_max, self.travel_creep_min_dist,
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
            # YUMI: travel creep transform. Registered on klippy:connect, once
            # every [extras] section (bed_mesh, skew_correction...) has had
            # its own __init__ run and called set_move_transform -- force=True
            # then hands US whatever was already chained, so we WRAP it
            # (outermost) instead of racing config load order. Only ONE
            # PrinterExtruder does this (the primary 'extruder'): E is a
            # single shared gcode axis regardless of which physical stepper
            # is synced to it.
            self.printer.register_event_handler(
                "klippy:connect", self._register_travel_creep)
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
    def _register_travel_creep(self):
        gcode_move = self.printer.lookup_object('gcode_move')
        self._creep_old_transform = gcode_move.set_move_transform(
            self, force=True)
    def get_position(self):
        # YUMI: must return the position in GCODE's OWN frame -- the one
        # bug the bench (scripts/travel_creep_bench.py, case C) caught: gcode_move
        # tracks last_position purely from parsed G-code text, incrementally,
        # and NEVER re-reads it from us between two moves (only on a reset).
        # If we reported the RAW downstream E (creep included), a second
        # consecutive travel would see a fake "de" against our injection and
        # be misread as a real extrusion resuming -> spurious repayment. Same
        # principle as bed_mesh's own get_position() undoing its Z adjustment:
        # report as if THIS layer had not touched anything.
        pos = self._creep_old_transform.get_position()
        steppers = self._backlash_steppers()
        if steppers and steppers[0]._creep_owed:
            pos[3] += steppers[0]._creep_owed
        return pos
    def move(self, newpos, speed):
        # YUMI: TRAVEL CREEP. Every move funnels through here before the rest
        # of the transform chain (bed_mesh, etc. below us) and the toolhead.
        # See travel_creep_rate/_max/_min_dist in ExtruderStepper.__init__ for
        # the "why". Mechanism, in one pass, no lookahead of our own needed:
        #   - E unchanged, XY travel >= min_dist  -> inject a bit MORE
        #     retraction into THIS move (capped so total owed <= max).
        #   - E growing (real extrusion resuming) and something is owed
        #     -> repay it ALL on this move, so the net material stays exact.
        #   - anything else (a real retract/reprise move, the take-up's own
        #     territory; a short travel under min_dist) -> untouched.
        # These are DISJOINT sets of moves (E==0 here vs E!=0 in
        # note_extrude_dir/process_move), so this never fights the take-up.
        steppers = self._backlash_steppers()
        if steppers and (steppers[0].travel_creep_rate > 0.
                         or steppers[0]._creep_owed > 0.):
            es = steppers[0]
            # self.get_position() (NOT the raw chain) -- it undoes our own
            # pending injection, the exact frame gcode_move's last_position
            # is tracked in (gcode_move never re-reads it between two moves,
            # so the frames must line up or the NEXT move misreads "de").
            cur = self.get_position()
            de = newpos[3] - cur[3]
            newpos = list(newpos)
            if abs(de) < .000001:
                # Travel (E unchanged in gcode's frame). Maybe grow the debt,
                # then ALWAYS re-apply the FULL current debt to translate
                # gcode's frame into the chain's -- even when this specific
                # move adds nothing new (already at the cap): the chain is
                # still holding a past injection, and re-sending gcode's raw
                # value untranslated would silently erase it (the bug the
                # bench caught: a capped second travel snapped E back to 0).
                if es.travel_creep_rate > 0.:
                    dx, dy = newpos[0] - cur[0], newpos[1] - cur[1]
                    dist = math.sqrt(dx * dx + dy * dy)
                    if dist >= es.travel_creep_min_dist:
                        creep = min(es.travel_creep_rate * dist,
                                   es.travel_creep_max - es._creep_owed)
                        if creep > 0.:
                            es._creep_owed += creep
                newpos[3] -= es._creep_owed
            elif es._creep_owed > 0.:
                # Any REAL E movement, extrude or retract -- settle the debt
                # first rather than let it compound with the take-up's own
                # offset. In practice this is almost always an extrude (the
                # print resuming); a bare retract landing here means no
                # extrusion happened between the travel and it, unlikely but
                # handled the same way either direction.
                newpos[3] += es._creep_owed
                es._creep_owed = 0.
        self._creep_old_transform.move(newpos, speed)
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
                es.note_extrude_dir(print_time - lead, axis_r,
                                    abs(move.axes_d[ea_index]))
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
