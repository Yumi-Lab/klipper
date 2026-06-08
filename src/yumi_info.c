// Yumi : constantes gravees dans le firmware, lues cote host via
// printer.mcu.mcu_constants.YUMI_CONFIG / .YUMI_COMMENT
#include "command.h"   // DECL_CONSTANT_STR
#include "autoconf.h"  // CONFIG_*
DECL_CONSTANT_STR("YUMI_CONFIG", CONFIG_YUMI_CONFIG);
DECL_CONSTANT_STR("YUMI_COMMENT", CONFIG_YUMI_COMMENT);
