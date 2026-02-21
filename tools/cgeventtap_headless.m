#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static FILE* g_log = NULL;
static CFMachPortRef g_event_tap = NULL;
static uint64_t g_counter = 0;

static void write_line_dec(const char* kind, long long value) {
  if (!g_log) return;
  g_counter++;
  fprintf(g_log, "%06llu    %s %lld\n", (unsigned long long)g_counter, kind, value);
  fflush(g_log);
}

static void write_line_hex(const char* kind, unsigned long long value) {
  if (!g_log) return;
  g_counter++;
  fprintf(g_log, "%06llu    %s 0x%llx\n", (unsigned long long)g_counter, kind, value);
  fflush(g_log);
}

static CGEventRef tap_callback(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void* refcon) {
  (void)proxy;
  (void)refcon;

  if (type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput) {
    if (g_event_tap) {
      CGEventTapEnable(g_event_tap, true);
    }
    return event;
  }

  switch (type) {
    case kCGEventKeyDown:
      write_line_dec("keyDown", CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode));
      break;
    case kCGEventKeyUp:
      write_line_dec("keyUp", CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode));
      break;
    case kCGEventFlagsChanged:
      write_line_hex("flagsChanged", (unsigned long long)CGEventGetFlags(event));
      break;
    default:
      break;
  }

  return event;
}

static void handle_signal(int signo) {
  (void)signo;
  if (g_event_tap) {
    CGEventTapEnable(g_event_tap, false);
  }
  CFRunLoopStop(CFRunLoopGetMain());
}

static const char* parse_log_path(int argc, char** argv) {
  const char* default_path = "/tmp/cgeventtap.log";
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "--log") == 0 && (i + 1) < argc) {
      return argv[i + 1];
    }
  }
  return default_path;
}

int main(int argc, char** argv) {
  const char* log_path = parse_log_path(argc, argv);
  g_log = fopen(log_path, "a");
  if (!g_log) {
    fprintf(stderr, "failed to open log path: %s\n", log_path);
    return 1;
  }

  signal(SIGINT, handle_signal);
  signal(SIGTERM, handle_signal);

  const CGEventMask mask = CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventKeyUp) |
                           CGEventMaskBit(kCGEventFlagsChanged);
  g_event_tap = CGEventTapCreate(
      kCGHIDEventTap, kCGTailAppendEventTap, kCGEventTapOptionListenOnly, mask, tap_callback, NULL);

  if (!g_event_tap) {
    fprintf(stderr,
            "failed to create event tap (check Input Monitoring / Accessibility permissions)\n");
    fclose(g_log);
    g_log = NULL;
    return 2;
  }

  CFRunLoopSourceRef source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, g_event_tap, 0);
  CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes);
  CFRelease(source);
  CGEventTapEnable(g_event_tap, true);

  fprintf(stderr, "seq-cgeventtap-headless running (log=%s)\n", log_path);
  CFRunLoopRun();

  if (g_event_tap) {
    CFRelease(g_event_tap);
    g_event_tap = NULL;
  }
  if (g_log) {
    fclose(g_log);
    g_log = NULL;
  }
  return 0;
}

