#pragma once

#include "options.h"

namespace seqd {
int run_daemon(const Options& opts);
// Fast, no-UI app switch using seqd's tracked previous front app.
// Returns true if an app was activated.
bool activate_previous_front_app_fast();
}  // namespace seqd
