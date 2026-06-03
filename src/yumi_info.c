// Yumi : constante de configuration gravee dans le firmware, lue cote host
// via mcu.get_constant("YUMI_CONFIG") / printer.mcu.mcu_constants.YUMI_CONFIG
#include "command.h"   // DECL_CONSTANT_STR
#include "autoconf.h"  // CONFIG_*
DECL_CONSTANT_STR("YUMI_CONFIG", CONFIG_YUMI_CONFIG);
