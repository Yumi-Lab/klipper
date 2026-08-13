// Banc de NON-REGRESSION du rattrapage de jeu bowden (YUMI).
//
// Exerce le VRAI code de klippy/chelper/kin_extruder.c (backlash_lookup,
// extruder_backlash_flip, extruder_set_backlash) pour mesurer la continuite
// de l'offset de rattrapage quand deux jalons sont espaces de moins que la
// rampe -- le cas "changement de couche dense" qui produisait
// "stepcompress: Invalid sequence" en impression reelle (voir GOAL.md /
// PROGRESS.md, Lots 2 et 3).
//
// Compilation (hote, aucun .dict ni docker requis) :
//     cc -O2 -w -I klippy/chelper -o .loop/tmp/bl_overlap_bench \
//        scripts/backlash_overlap_bench.c -lm && .loop/tmp/bl_overlap_bench
//
// CRITERE (meme run, bras de reference) : le saut max de chaque cas dense
// doit rester sous max(1 pas, 4x le bras de controle B). Origine du seuil :
// stepcompress refuse > 1 pas a intervalle nul (stepcompress.c check_line,
// CHECK_LINES=1) -- une vraie discontinuite vaut au moins 2 pas a TOUT
// echantillonnage, tandis qu'une pente legitime (<= 1,5*jeu/rampe) echanti-
// llonnee a 10 us reste sous 0,5 pas. Le facteur 4 sur le bras B couvre la
// difference de pente legitime entre cas denses et cas espace. Si le bras B
// lui-meme depassait le seuil, c'est le SEUIL qui serait en defaut, pas le
// produit.
//
// Sortie : rc=0 si tous les cas sont continus, rc=1 sinon.
#include <stdio.h>
#include <math.h>
#include "kin_extruder.c"   // code reel sous test (fonctions static accessibles)

// --- stubs pour satisfaire l'edition de liens (jamais appeles ici) ---
void errorf(const char *fmt, ...) { (void)fmt; }
double get_monotonic(void) { return 0.; }
double move_get_distance(struct move *m, double move_time)
{ (void)m; (void)move_time; return 0.; }

#define STEPS_PER_MM 400.0   // extrudeur demultiplie typique (BMG ~415)
#define SAMPLE_DT    0.00001 // 10 us : sous la pente legitime la plus raide

// Rejoue une sequence de jalons (cible, print_time) et mesure le plus grand
// ecart d'offset entre deux echantillons consecutifs de backlash_lookup.
// flush_back_frac positionne le flush simule derriere le jalon precedent
// (1.0 = une rampe entiere -> chemin de fusion ; 0.5 = generation DEJA dans
// la fenetre precedente -> chemin de raccourcissement de rampe).
// Retourne le saut max en PAS.
static double run_case(const char *name, double play, double ramp,
                       const double (*flips)[2], int n,
                       double t0, double t1, double flush_back_frac)
{
    struct stepper_kinematics *sk = extruder_stepper_alloc();
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    extruder_set_backlash(sk, play, ramp);
    extruder_backlash_reset(sk);
    for (int i = 0; i < n; i++) {
        // la generation de pas suit derriere le planner : dernier flush un
        // peu avant le jalon precedent.
        sk->last_flush_time = (i > 0 ? flips[i-1][1] : t0)
                              - ramp * flush_back_frac;
        extruder_backlash_flip(sk, flips[i][1], flips[i][0], ramp);
    }
    double prev = backlash_lookup(&es->bl_list, t0, ramp);
    double max_jump = 0., jump_at = 0.;
    for (double t = t0 + SAMPLE_DT; t <= t1; t += SAMPLE_DT) {
        double v = backlash_lookup(&es->bl_list, t, ramp);
        double d = fabs(v - prev);
        if (d > max_jump) { max_jump = d; jump_at = t; }
        prev = v;
    }
    printf("%-36s play=%.4f ramp=%.1fms jalons=%d\n",
           name, play, ramp*1000., n);
    printf("  saut max = %.6f mm @ t=%.4f s  = %.2f pas @ %.0f pas/mm"
           "  (seuil crash: > 1 pas a intervalle 0)\n",
           max_jump, jump_at, max_jump * STEPS_PER_MM, STEPS_PER_MM);
    // Discontinuite exacte a chaque jalon du milieu : offset juste avant vs
    // juste a son print_time. Un ecart >> pente legitime est un SAUT.
    printf("  offset(t-100us) -> offset(t) a chaque jalon :\n");
    for (int i = 0; i < n; i++) {
        double tm = flips[i][1];
        double avant = backlash_lookup(&es->bl_list, tm - 0.0001, ramp);
        double apres = backlash_lookup(&es->bl_list, tm, ramp);
        printf("    jalon %d t=%.3f cible=%+.4f : %+.6f -> %+.6f"
               "  ecart=%+.6f mm\n",
               i, tm, flips[i][0], avant, apres, apres - avant);
    }
    extruder_stepper_free(sk);
    return max_jump * STEPS_PER_MM;
}

int main(void)
{
    // Config exacte du crash : BOWDEN_LENGTH=800 COEF=1 SPEED=40 ACCEL=15000
    // DEDUCT=0.5 BLEED=10  ->  play=1.5708 mm, ramp=58.9 ms.
    const double play = 1.5708, ramp = 0.0589;

    // CAS A — changement de couche dense (petite piece, 100 mm/s, segments
    // ~2 mm = 20 ms) : retract 1.000 / reprise 1.055 (target -deduct) /
    // paliers bleed toutes les ~20 ms / retract suivant 20 ms apres le
    // dernier palier. Cibles calculees par la logique de note_extrude_dir.
    // Flush une rampe derriere : exerce le chemin de FUSION des jalons.
    static const double dense[][2] = {
        {-1.5708, 1.000},   // retraction : jeu entier
        {-0.5000, 1.055},   // reprise : tout sauf la deduction (55 ms apres)
        {-0.4375, 1.075},   // palier 7/8 (20 ms)
        {-0.3125, 1.095},   // palier 5/8 (20 ms)
        {-0.2500, 1.115},   // palier 4/8
        {-0.1250, 1.135},   // palier 2/8
        { 0.0000, 1.155},   // bleed termine
        {-1.5708, 1.175},   // retraction suivante (20 ms apres le palier 0)
    };
    double a = run_case("A dense (chgt couche, fusion)", play, ramp,
                        dense, 8, 0.90, 1.30, 1.0);

    // CAS B — controle : memes cibles, jalons espaces de 200 ms (> rampe).
    // Bras de REFERENCE du meme run : le code est innocent hors recouvrement.
    static const double spaced[][2] = {
        {-1.5708, 1.000},
        {-0.5000, 1.200},
        {-0.4375, 1.400},
        {-0.3125, 1.600},
        {-0.2500, 1.800},
        {-0.1250, 2.000},
        { 0.0000, 2.200},
        {-1.5708, 2.400},
    };
    double b = run_case("B controle (jalons > rampe)", play, ramp,
                        spaced, 8, 0.90, 2.55, 1.0);

    // CAS C — dense mais avec la rampe par defaut (SPEED=120 -> 19.6 ms).
    double c = run_case("C dense, rampe defaut 19.6ms", play, 0.0196,
                        dense, 8, 0.90, 1.30, 1.0);

    // CAS D — dense, flush DANS la fenetre precedente (une demi-rampe
    // derriere) : la generation a deja commence a suivre la rampe du jalon
    // precedent, la fusion est interdite -> exerce le chemin de
    // RACCOURCISSEMENT de la nouvelle rampe.
    double d = run_case("D dense, flush mi-fenetre", play, ramp,
                        dense, 8, 0.90, 1.30, 0.5);

    // Seuil : voir l'en-tete. Bras de reference B mesure dans CE run.
    double seuil = 4. * b;
    if (seuil < 1.)
        seuil = 1.;     // plancher : un vrai saut vaut >= 2 pas a tout
                        // echantillonnage ; 1 pas = plancher de quantification
    printf("seuil (meme run) = %.2f pas  (4x controle B=%.2f, plancher 1)\n",
           seuil, b);
    int bad = 0;
    if (a > seuil) { printf("ECHEC cas A : %.2f pas > %.2f\n", a, seuil); bad = 1; }
    if (c > seuil) { printf("ECHEC cas C : %.2f pas > %.2f\n", c, seuil); bad = 1; }
    if (d > seuil) { printf("ECHEC cas D : %.2f pas > %.2f\n", d, seuil); bad = 1; }
    if (b > seuil) { printf("ECHEC bras de reference B : %.2f pas > %.2f"
                            " — le SEUIL est en defaut, pas le produit\n",
                            b, seuil); bad = 1; }
    if (bad)
        return 1;
    printf("CONTINUITE OK : tous les cas sous le seuil.\n");
    return 0;
}
