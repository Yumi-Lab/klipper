// Extruder stepper pulse time generation
//
// Copyright (C) 2018-2019  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stddef.h> // offsetof
#include <stdlib.h> // malloc
#include <string.h> // memset
#include "compiler.h" // __visible
#include "itersolve.h" // struct stepper_kinematics
#include "list.h" // list_node
#include "pyhelper.h" // errorf
#include "trapq.h" // move_get_distance

struct pa_params {
    double pressure_advance, active_print_time;
    struct list_node node;
};

// Without pressure advance, the extruder stepper position is:
//     extruder_position(t) = nominal_position(t)
// When pressure advance is enabled, additional filament is pushed
// into the extruder during acceleration (and retracted during
// deceleration). The formula is:
//     pa_position(t) = (nominal_position(t)
//                       + pressure_advance * nominal_velocity(t))
// Which is then "smoothed" using a weighted average:
//     smooth_position(t) = (
//         definitive_integral(pa_position(x) * (smooth_time/2 - abs(t-x)) * dx,
//                             from=t-smooth_time/2, to=t+smooth_time/2)
//         / ((smooth_time/2)**2))

// Calculate the definitive integral of the motion formula:
//   position(t) = base + t * (start_v + t * half_accel)
static double
extruder_integrate(double base, double start_v, double half_accel
                   , double start, double end)
{
    double half_v = .5 * start_v, sixth_a = (1. / 3.) * half_accel;
    double si = start * (base + start * (half_v + start * sixth_a));
    double ei = end * (base + end * (half_v + end * sixth_a));
    return ei - si;
}

// Calculate the definitive integral of time weighted position:
//   weighted_position(t) = t * (base + t * (start_v + t * half_accel))
static double
extruder_integrate_time(double base, double start_v, double half_accel
                        , double start, double end)
{
    double half_b = .5 * base, third_v = (1. / 3.) * start_v;
    double eighth_a = .25 * half_accel;
    double si = start * start * (half_b + start * (third_v + start * eighth_a));
    double ei = end * end * (half_b + end * (third_v + end * eighth_a));
    return ei - si;
}

// Calculate the definitive integral of extruder for a given move
static double
pa_move_integrate(struct move *m, struct list_head *pa_list
                  , double base, double start, double end, double time_offset)
{
    if (start < 0.)
        start = 0.;
    if (end > m->move_t)
        end = m->move_t;
    // Determine pressure_advance value
    int can_pressure_advance = m->axes_r.y != 0.;
    double pressure_advance = 0.;
    if (can_pressure_advance) {
        struct pa_params *pa = list_last_entry(pa_list, struct pa_params, node);
        while (unlikely(pa->active_print_time > m->print_time) &&
                !list_is_first(&pa->node, pa_list)) {
            pa = list_prev_entry(pa, node);
        }
        pressure_advance = pa->pressure_advance;
    }
    // Calculate base position and velocity with pressure advance
    base += pressure_advance * m->start_v;
    double start_v = m->start_v + pressure_advance * 2. * m->half_accel;
    // Calculate definitive integral
    double ha = m->half_accel;
    double iext = extruder_integrate(base, start_v, ha, start, end);
    double wgt_ext = extruder_integrate_time(base, start_v, ha, start, end);
    return wgt_ext - time_offset * iext;
}

// Calculate the definitive integral of the extruder over a range of moves
static double
pa_range_integrate(struct move *m, double move_time
                   , struct list_head *pa_list, double hst)
{
    // Calculate integral for the current move
    double res = 0., start = move_time - hst, end = move_time + hst;
    double start_base = m->start_pos.x;
    res += pa_move_integrate(m, pa_list, 0., start, move_time, start);
    res -= pa_move_integrate(m, pa_list, 0., move_time, end, end);
    // Integrate over previous moves
    struct move *prev = m;
    while (unlikely(start < 0.)) {
        prev = list_prev_entry(prev, node);
        start += prev->move_t;
        double base = prev->start_pos.x - start_base;
        res += pa_move_integrate(prev, pa_list, base, start
                                 , prev->move_t, start);
    }
    // Integrate over future moves
    while (unlikely(end > m->move_t)) {
        end -= m->move_t;
        m = list_next_entry(m, node);
        double base = m->start_pos.x - start_base;
        res -= pa_move_integrate(m, pa_list, base, 0., end, end);
    }
    return res;
}

struct extruder_stepper {
    struct stepper_kinematics sk;
    struct list_head pa_list;
    double half_smooth_time, inv_half_smooth_time2;
    // YUMI: bowden backlash take-up (see comment below)
    struct list_head bl_list;
    double backlash_play, backlash_ramp;
};

// YUMI -- BOWDEN BACKLASH TAKE-UP
//
// In a curved bowden the compressed filament rests against the outer wall and,
// under tension, against the inner one. Reversing direction therefore costs a
// DEAD TRAVEL of  play = (tube_id - filament_diameter) * total_curvature
// before anything is transmitted at all. Until that travel is done, pressure
// advance, smooth_time and lead_time push against nothing: their command is
// swallowed by the gap and never reaches the melt.
//
// This layer walks that gap AHEAD of time, so it is finished exactly when the
// real extrusion starts. It is an ENABLER, not a compensator: on its own it
// deposits nothing -- travelling the gap transmits nothing -- but it makes the
// other three effective again.
//
// State is a square wave: 0 while pushing, -play while pulling. The ramp toward
// the next state is anticipated so that it LANDS on the reversal, never after.
// Its duration is play / speed -- a couple of millimetres at a hundred mm/s, so
// a handful of milliseconds. That bound matters: the scan window it claims stays
// far inside what the smoothing already asks for, which is why this cannot push
// step generation out of its sliding window (the failure mode documented in
// YUMI_PATCHES.md as "Invalid sequence").
// The direction history, stamped by the planner. A position callback has no
// memory: after a 40 ms pause it can no longer see which way the filament last
// went, and reading further back means reading moves Klipper may already have
// freed -- the very fault that produces "Invalid sequence". So the planner, which
// walks the moves in order and therefore KNOWS, records each reversal here.
// Same shape as pa_list, for the same reason.
struct backlash_params {
    double target, print_time, ramp;
    struct list_node node;
};

// Plancher defensif pour la rampe raccourcie a l'insertion : jamais nulle,
// sinon backlash_lookup retombe sur la rampe courante (cf. le commentaire du
// garde-fou dans extruder_backlash_flip). 1 us : sous tout echantillonnage
// de generation de pas, donc sans effet mesurable -- l'invariant seul compte.
#define BACKLASH_MIN_RAMP 0.000001

// Offset at `print_time`: hold the current target, and ramp toward the next one
// so the ramp LANDS on the reversal instead of starting there. That is the whole
// point -- the gap must be walked before the real extrusion begins, not during.
// Chaque jalon porte SA rampe. Utiliser la rampe courante pour interpoler un
// jalon pose avant elle ferait sauter l'offset des qu'on change la vitesse ou
// l'acceleration en cours d'impression : a instant egal, (ramp-dt)/ramp rend une
// fraction differente. Saut de position, "Internal error in stepcompress".
static double
backlash_lookup(struct list_head *bl_list, double print_time, double ramp)
{
    struct backlash_params *bp = list_last_entry(bl_list,
                                                 struct backlash_params, node);
    while (unlikely(bp->print_time > print_time)
           && !list_is_first(&bp->node, bl_list)) {
        struct backlash_params *prev = list_prev_entry(bp, node);
        if (prev->print_time > print_time) {
            bp = prev;
            continue;
        }
        // `bp` is the upcoming reversal, `prev` the state we are still in.
        double dt = bp->print_time - print_time;
        double r = bp->ramp > 0. ? bp->ramp : ramp;
        if (dt >= r)
            return prev->target;
        // Profil en S plutot qu'une pente droite. Une rampe LINEAIRE fait sauter
        // la vitesse du moteur de 0 a sa valeur d'un coup, deux fois par
        // inversion : sur un extrudeur demultiplie 50:17 cela veut dire des
        // centaines de tours/minute appliques sans transition -- le moteur
        // claque, encaisse un a-coup, et peut perdre des pas sans le signaler.
        // f(u) = u^2 (3 - 2u) part et arrive a vitesse NULLE. Symetrique, donc
        // f(1/2) = 1/2 : la mi-rampe reste a la moitie du jeu.
        double u = (r - dt) / r;
        double sm = u * u * (3. - 2. * u);
        return prev->target + (bp->target - prev->target) * sm;
    }
    return bp->target;
}

static double
extruder_calc_position(struct stepper_kinematics *sk, struct move *m
                       , double move_time)
{
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    double hst = es->half_smooth_time;
    double pos;
    if (!hst) {
        // Pressure advance not enabled
        pos = m->start_pos.x + move_get_distance(m, move_time);
    } else {
        // Apply pressure advance and average over smooth_time
        double area = pa_range_integrate(m, move_time, &es->pa_list, hst);
        pos = m->start_pos.x + area * es->inv_half_smooth_time2;
    }
    // YUMI: additive backlash take-up -- neutral when the play is zero, which is
    // the case whenever no bowden length is declared.
    // On ne teste QUE la rampe, jamais le jeu. Couper la couche parce que le jeu
    // est passe a zero ferait disparaitre d'un coup un decalage qui vaut encore
    // -jeu : la position commandee sauterait de plusieurs millimetres et
    // stepcompress ne sait pas emettre un saut ("Internal error in stepcompress",
    // machine arretee). A jeu nul le planner empile simplement une cible 0, et
    // l'offset y redescend par sa propre rampe -- continu, donc emettable.
    if (es->backlash_ramp > 0.)
        pos += backlash_lookup(&es->bl_list, m->print_time + move_time,
                               es->backlash_ramp);
    return pos;
}

// YUMI: the scan window itersolve must keep populated -- the smoothing needs
// half_smooth_time on both sides, the take-up a few ms each way. Written
// in ONE place so the two layers cannot disagree about what they require.
static void
extruder_update_scan_window(struct extruder_stepper *es)
{
    double hst = es->half_smooth_time;
    double win = hst;
    // The take-up reads a few milliseconds on both sides: back to know which way
    // the filament last went, forward to land the ramp on the reversal.
    if (es->backlash_play > 0. && es->backlash_ramp > win)
        win = es->backlash_ramp;
    es->sk.gen_steps_pre_active = win;
    es->sk.gen_steps_post_active = win;
}

void __visible
extruder_set_backlash(struct stepper_kinematics *sk, double play, double ramp)
{
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    es->backlash_play = play;
    es->backlash_ramp = ramp;
    extruder_update_scan_window(es);
}

// Remet l'historique a plat. A appeler quand le stepper CHANGE DE FILE : sync_to_
// extruder fait un set_position() qui ignore l'offset, donc un jalon survivant de
// l'attache precedente ferait reapparaitre le decalage d'un coup -- saut de
// position, "Internal error in stepcompress", machine arretee en pleine impression.
void __visible
extruder_backlash_reset(struct stepper_kinematics *sk)
{
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    while (!list_empty(&es->bl_list)) {
        struct backlash_params *bp = list_first_entry(
                &es->bl_list, struct backlash_params, node);
        list_del(&bp->node);
        free(bp);
    }
    struct backlash_params *bp = malloc(sizeof(*bp));
    memset(bp, 0, sizeof(*bp));
    list_add_tail(&bp->node, &es->bl_list);
}

// Called by the planner when the commanded extruder direction flips. `target` is
// where the take-up must stand once the reversal is reached: 0 when the filament
// will push (the gap is closed ahead of it), -play when it will pull.
void __visible
extruder_backlash_flip(struct stepper_kinematics *sk, double print_time
                       , double target, double ramp)
{
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    struct backlash_params *last = list_last_entry(
            &es->bl_list, struct backlash_params, node);
    if (last->target == target)
        return;                         // nothing changed, keep the list short
    // YUMI: backlash_lookup suppose que les fenetres de rampe ne se
    // RECOUVRENT JAMAIS : la transition vers un jalon doit avoir atterri
    // (a son print_time) avant que la rampe du suivant ne demarre (a
    // print_time - ramp). Sinon, au print_time du jalon du milieu, la paire
    // active bascule et l'offset SAUTE de (cible_suivante - cible_milieu) *
    // f(1 - dt/ramp) : discontinuite de la position commandee, itersolve
    // place plusieurs pas au meme instant, stepcompress refuse
    // ("Invalid sequence", machine arretee). C'est le crash du changement de
    // couche : retract, reprise et paliers de bleed tombent volontiers a
    // moins d'une rampe les uns des autres. On impose donc l'invariant ICI,
    // a l'insertion -- la continuite d'abord :
    double start = print_time - ramp;
    while (!list_is_first(&last->node, &es->bl_list)
           && start < last->print_time) {
        if (last->print_time - last->ramp < sk->last_flush_time) {
            // Trop tard pour supprimer `last` : la generation de pas est DEJA
            // entree dans sa fenetre, retirer le jalon changerait la position
            // commandee a des instants deja emis. On raccourcit alors la
            // NOUVELLE rampe pour qu'elle demarre pile a l'atterrissage de
            // `last` : a cet instant f(0)=0 et la base vaut last->target,
            // la continuite est assuree des deux cotes. La pointe de vitesse
            // monte (1,5*delta/ramp) -- un a-coup possible, jamais un saut :
            // la continuite prime sur le confort moteur.
            ramp = print_time - last->print_time;
            // Garde-fou : deux flips au MEME print_time donneraient une rampe
            // nulle, et backlash_lookup retomberait alors sur la rampe
            // COURANTE (`bp->ramp > 0. ? bp->ramp : ramp`) -- la transition
            // serait interpolee avec la rampe d'une autre epoque si
            // BACKLASH_SPEED a change entre-temps, exactement le piege du
            // fix 0bf594f ("chaque jalon porte SA rampe"). Le planner
            // produit des print_time strictement croissants (un flip exige
            // un move extrudant, donc une duree), ce cas est donc purement
            // defensif : une micro-rampe preserve l'invariant sans rien
            // changer au comportement (a print_time egaux, le balayage de
            // backlash_lookup saute ce jalon, sa rampe n'est jamais lue).
            if (ramp < BACKLASH_MIN_RAMP)
                ramp = BACKLASH_MIN_RAMP;
            break;
        }
        // `last` est entierement dans le futur (sa fenetre n'a pas encore ete
        // generee) : sa cible ne sera de toute facon jamais atteinte, la
        // nouvelle rampe la supplante. Fusionner plutot qu'empiler deux
        // rampes qui se chevauchent.
        struct backlash_params *prev = list_prev_entry(last, node);
        list_del(&last->node);
        free(last);
        last = prev;
    }
    // Drop entries the flush has moved past, so the list cannot grow unbounded.
    double cleanup = sk->last_flush_time - es->backlash_ramp;
    struct backlash_params *first = list_first_entry(
            &es->bl_list, struct backlash_params, node);
    while (!list_is_last(&first->node, &es->bl_list)) {
        struct backlash_params *next = list_next_entry(first, node);
        if (next->print_time >= cleanup)
            break;
        list_del(&first->node);
        free(first);
        first = next;
    }
    struct backlash_params *bp = malloc(sizeof(*bp));
    memset(bp, 0, sizeof(*bp));
    bp->target = target;
    bp->print_time = print_time;
    bp->ramp = ramp;
    list_add_tail(&bp->node, &es->bl_list);
}

void __visible
extruder_set_pressure_advance(struct stepper_kinematics *sk, double print_time
                              , double pressure_advance, double smooth_time)
{
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    double hst = smooth_time * .5, old_hst = es->half_smooth_time;
    es->half_smooth_time = hst;
    // YUMI: the take-up also claims a window -- one writer, so they agree
    extruder_update_scan_window(es);

    // Cleanup old pressure advance parameters
    double cleanup_time = sk->last_flush_time - (old_hst > hst ? old_hst : hst);
    struct pa_params *first_pa = list_first_entry(
            &es->pa_list, struct pa_params, node);
    while (!list_is_last(&first_pa->node, &es->pa_list)) {
        struct pa_params *next_pa = list_next_entry(first_pa, node);
        if (next_pa->active_print_time >= cleanup_time) break;
        list_del(&first_pa->node);
        first_pa = next_pa;
    }

    if (! hst)
        return;
    es->inv_half_smooth_time2 = 1. / (hst * hst);

    if (list_last_entry(&es->pa_list, struct pa_params, node)->pressure_advance
            == pressure_advance) {
        // Retain old pa_params
        return;
    }
    // Add new pressure advance parameters
    struct pa_params *pa = malloc(sizeof(*pa));
    memset(pa, 0, sizeof(*pa));
    pa->pressure_advance = pressure_advance;
    pa->active_print_time = print_time;
    list_add_tail(&pa->node, &es->pa_list);
}

struct stepper_kinematics * __visible
extruder_stepper_alloc(void)
{
    struct extruder_stepper *es = malloc(sizeof(*es));
    memset(es, 0, sizeof(*es));
    es->sk.calc_position_cb = extruder_calc_position;
    es->sk.active_flags = AF_X;
    list_init(&es->pa_list);
    list_init(&es->bl_list);
    struct backlash_params *bp = malloc(sizeof(*bp));
    memset(bp, 0, sizeof(*bp));
    list_add_tail(&bp->node, &es->bl_list);
    struct pa_params *pa = malloc(sizeof(*pa));
    memset(pa, 0, sizeof(*pa));
    list_add_tail(&pa->node, &es->pa_list);
    return &es->sk;
}

void __visible
extruder_stepper_free(struct stepper_kinematics *sk)
{
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    while (!list_empty(&es->pa_list)) {
        struct pa_params *pa = list_first_entry(
                &es->pa_list, struct pa_params, node);
        list_del(&pa->node);
        free(pa);
    }
    while (!list_empty(&es->bl_list)) {
        struct backlash_params *bp = list_first_entry(
                &es->bl_list, struct backlash_params, node);
        list_del(&bp->node);
        free(bp);
    }
    free(sk);
}
