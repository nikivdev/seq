#pragma once

#include "options.h"

namespace action_pack_server {

// Starts the action-pack server if enabled in opts (opts.action_pack_listen non-empty).
// Intended to be run from inside seqd (daemon process).
void start_in_background(const Options& opts);

}  // namespace action_pack_server

