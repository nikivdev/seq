#pragma once

struct Options;

// Implements `seq action-pack ...` CLI subcommands.
int cmd_action_pack(int argc, char** argv, int index, const Options& opts);

