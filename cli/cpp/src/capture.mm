#include "capture.h"
#include "context.h"
#include "metrics.h"
#include "process.h"
#include "trace.h"
#include "base.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <sys/stat.h>

#import <Cocoa/Cocoa.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreImage/CoreImage.h>
#import <ImageIO/ImageIO.h>
#import <Vision/Vision.h>

#include <sqlite3.h>

namespace capture {
namespace {

// ---------------------------------------------------------------------------
//  Globals
// ---------------------------------------------------------------------------

static Config g_cfg;

static std::atomic<bool> g_running{false};

static SCStream*          g_stream   = nil;
static dispatch_queue_t   g_cap_q    = nil;
static dispatch_queue_t   g_ocr_q    = nil;

static sqlite3*    g_fts_db = nullptr;
static std::mutex  g_fts_mu;

static NSString*   g_spool_ns = nil;

// Adaptive sampling state (accessed only on g_cap_q).
static uint64_t g_last_capture_ms  = 0;
static double   g_interval_ms      = 2000;
static uint64_t g_last_hash        = 0;
static constexpr double kMinInterval = 2000;
static constexpr double kMaxInterval = 120000;

// Sync timer state.
static dispatch_source_t g_sync_timer = nil;

// ---------------------------------------------------------------------------
//  Utilities
// ---------------------------------------------------------------------------

uint64_t now_ms() {
  using namespace std::chrono;
  return (uint64_t)duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

// ---------------------------------------------------------------------------
//  Perceptual hash — 8x9 luma grid → 64-bit hash.
// ---------------------------------------------------------------------------

uint64_t phash(CVPixelBufferRef buf) {
  OSType fmt = CVPixelBufferGetPixelFormatType(buf);
  CVPixelBufferLockBaseAddress(buf, kCVPixelBufferLock_ReadOnly);

  uint8_t* base  = nullptr;
  size_t   width = 0, height = 0, stride = 0;

  // Prefer luma plane (NV12).
  if (fmt == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange ||
      fmt == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) {
    base   = (uint8_t*)CVPixelBufferGetBaseAddressOfPlane(buf, 0);
    width  = CVPixelBufferGetWidthOfPlane(buf, 0);
    height = CVPixelBufferGetHeightOfPlane(buf, 0);
    stride = CVPixelBufferGetBytesPerRowOfPlane(buf, 0);
  } else {
    // Fallback: treat first byte of each pixel as luminance (rough).
    base   = (uint8_t*)CVPixelBufferGetBaseAddress(buf);
    width  = CVPixelBufferGetWidth(buf);
    height = CVPixelBufferGetHeight(buf);
    stride = CVPixelBufferGetBytesPerRow(buf);
  }

  uint64_t hash = 0;
  if (!base || width < 9 || height < 9) {
    CVPixelBufferUnlockBaseAddress(buf, kCVPixelBufferLock_ReadOnly);
    return hash;
  }

  uint8_t grid[9][8];
  for (int gy = 0; gy < 9; gy++) {
    size_t y = (size_t)((double)gy / 8.0 * (double)(height - 1));
    for (int gx = 0; gx < 8; gx++) {
      size_t x = (size_t)((double)gx / 7.0 * (double)(width - 1));
      grid[gy][gx] = base[y * stride + x];
    }
  }

  CVPixelBufferUnlockBaseAddress(buf, kCVPixelBufferLock_ReadOnly);

  int bit = 0;
  for (int y = 0; y < 8; y++) {
    for (int x = 0; x < 8; x++) {
      if (grid[y][x] > grid[y + 1][x]) {
        hash |= (1ULL << bit);
      }
      bit++;
    }
  }
  return hash;
}

int hamming(uint64_t a, uint64_t b) {
  return __builtin_popcountll(a ^ b);
}

// ---------------------------------------------------------------------------
//  HEIC encoding via ImageIO
// ---------------------------------------------------------------------------

bool encode_heic(CVPixelBufferRef pixelBuffer, NSString* path) {
  @autoreleasepool {
    CIImage* ci = [CIImage imageWithCVPixelBuffer:pixelBuffer];
    if (!ci) return false;

    CIContext* ctx = [CIContext context];
    CGImageRef cg = [ctx createCGImage:ci fromRect:ci.extent];
    if (!cg) return false;

    NSURL* url = [NSURL fileURLWithPath:path];
    CGImageDestinationRef dest = CGImageDestinationCreateWithURL(
        (__bridge CFURLRef)url,
        (__bridge CFStringRef)@"public.heic",
        1, NULL);
    if (!dest) {
      CGImageRelease(cg);
      return false;
    }

    NSDictionary* opts = @{
      (__bridge NSString*)kCGImageDestinationLossyCompressionQuality: @0.7
    };
    CGImageDestinationAddImage(dest, cg, (__bridge CFDictionaryRef)opts);
    bool ok = CGImageDestinationFinalize(dest);
    CFRelease(dest);
    CGImageRelease(cg);
    return ok;
  }
}

// ---------------------------------------------------------------------------
//  Vision OCR
// ---------------------------------------------------------------------------

NSString* run_ocr(CVPixelBufferRef pixelBuffer) {
  @autoreleasepool {
    VNRecognizeTextRequest* request = [[VNRecognizeTextRequest alloc] init];
    request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
    request.usesLanguageCorrection = YES;

    VNImageRequestHandler* handler =
        [[VNImageRequestHandler alloc] initWithCVPixelBuffer:pixelBuffer options:@{}];
    NSError* error = nil;
    [handler performRequests:@[request] error:&error];
    if (error) return @"";

    NSMutableString* text = [NSMutableString string];
    for (VNRecognizedTextObservation* obs in request.results) {
      VNRecognizedText* top = [[obs topCandidates:1] firstObject];
      if (top.string.length > 0) {
        if (text.length > 0) [text appendString:@"\n"];
        [text appendString:top.string];
      }
    }
    return text;
  }
}

// ---------------------------------------------------------------------------
//  SQLite FTS5
// ---------------------------------------------------------------------------

bool init_fts(const char* path) {
  if (g_fts_db) return true;
  int rc = sqlite3_open(path, &g_fts_db);
  if (rc != SQLITE_OK) { g_fts_db = nullptr; return false; }
  const char* sql =
      "CREATE VIRTUAL TABLE IF NOT EXISTS frame_text "
      "USING fts5(ts_ms, app_name, window_title, ocr_text)";
  return sqlite3_exec(g_fts_db, sql, nullptr, nullptr, nullptr) == SQLITE_OK;
}

void insert_fts(uint64_t ts_ms, const std::string& app,
                const std::string& title, const std::string& ocr) {
  std::lock_guard<std::mutex> lock(g_fts_mu);
  if (!g_fts_db) return;
  sqlite3_stmt* stmt = nullptr;
  const char* sql =
      "INSERT INTO frame_text(ts_ms, app_name, window_title, ocr_text) VALUES(?,?,?,?)";
  if (sqlite3_prepare_v2(g_fts_db, sql, -1, &stmt, nullptr) != SQLITE_OK) return;
  std::string ts = std::to_string(ts_ms);
  sqlite3_bind_text(stmt, 1, ts.c_str(),    -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 2, app.c_str(),   -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 3, title.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 4, ocr.c_str(),   -1, SQLITE_TRANSIENT);
  sqlite3_step(stmt);
  sqlite3_finalize(stmt);
}

// ---------------------------------------------------------------------------
//  Spool management — keep local spool under budget.
// ---------------------------------------------------------------------------

void enforce_spool_limit() {
  @autoreleasepool {
    NSFileManager* fm = [NSFileManager defaultManager];
    NSDirectoryEnumerator* en = [fm enumeratorAtPath:g_spool_ns];
    if (!en) return;

    NSMutableArray<NSDictionary*>* files = [NSMutableArray array];
    uint64_t total = 0;
    NSString* rel;
    while ((rel = [en nextObject])) {
      if (![rel hasSuffix:@".heic"]) continue;
      NSString* full = [g_spool_ns stringByAppendingPathComponent:rel];
      NSDictionary* attrs = [fm attributesOfItemAtPath:full error:nil];
      if (!attrs) continue;
      uint64_t sz = [attrs fileSize];
      total += sz;
      [files addObject:@{
        @"p": full,
        @"d": (attrs.fileModificationDate ? attrs.fileModificationDate : [NSDate distantPast]),
        @"s": @(sz),
      }];
    }

    uint64_t limit = (uint64_t)g_cfg.max_spool_mb * 1024 * 1024;
    if (total <= limit) return;

    [files sortUsingComparator:^(NSDictionary* a, NSDictionary* b) {
      return [a[@"d"] compare:b[@"d"]];
    }];
    for (NSDictionary* f in files) {
      if (total <= limit) break;
      [fm removeItemAtPath:f[@"p"] error:nil];
      total -= [f[@"s"] unsignedLongLongValue];
    }
  }
}

// ---------------------------------------------------------------------------
//  SCStreamOutput handler
// ---------------------------------------------------------------------------

}  // namespace
}  // namespace capture

@interface SeqCaptureOutput : NSObject <SCStreamOutput, SCStreamDelegate>
@end

@implementation SeqCaptureOutput

- (void)stream:(SCStream*)stream
    didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
                   ofType:(SCStreamOutputType)type {
  if (type != SCStreamOutputTypeScreen) return;
  CVImageBufferRef pb = CMSampleBufferGetImageBuffer(sampleBuffer);
  if (!pb) return;

  uint64_t now = capture::now_ms();

  // Adaptive rate limiting.
  if (now - capture::g_last_capture_ms < (uint64_t)capture::g_interval_ms) return;

  uint64_t hash = capture::phash(pb);
  if (capture::hamming(hash, capture::g_last_hash) <= capture::g_cfg.hash_threshold) {
    // Screen stable — increase interval (exponential backoff).
    capture::g_interval_ms = std::min(capture::g_interval_ms * 1.5, capture::kMaxInterval);
    return;
  }

  // Screen changed — capture this frame.
  capture::g_last_hash       = hash;
  capture::g_interval_ms     = capture::kMinInterval;
  capture::g_last_capture_ms = now;

  // Thermal check.
  NSProcessInfoThermalState thermal = [NSProcessInfo processInfo].thermalState;
  if (thermal == NSProcessInfoThermalStateCritical) return;  // pause entirely
  if (thermal == NSProcessInfoThermalStateSerious) {
    capture::g_interval_ms = std::max(capture::g_interval_ms, capture::kMinInterval * 2);
  }

  // Date-based subdirectory.
  @autoreleasepool {
    NSDateFormatter* fmt = [[NSDateFormatter alloc] init];
    [fmt setDateFormat:@"yyyy-MM-dd"];
    NSString* dateStr = [fmt stringFromDate:[NSDate date]];
    NSString* dayDir  = [capture::g_spool_ns stringByAppendingPathComponent:dateStr];
    [[NSFileManager defaultManager] createDirectoryAtPath:dayDir
                              withIntermediateDirectories:YES
                                               attributes:nil
                                                    error:nil];

    NSString* framePath = [dayDir stringByAppendingPathComponent:
        [NSString stringWithFormat:@"%llu.heic", now]];

    // Retain pixel buffer for async work.
    CVPixelBufferRetain(pb);

    context::WindowCtx ctx = context::current_window();
    std::string app   = ctx.app_name;
    std::string title = ctx.window_title;
    std::string fpath = [framePath UTF8String];

    // Encode + OCR on background queue.
    dispatch_async(capture::g_ocr_q, ^{
      @autoreleasepool {
        bool ok = capture::encode_heic(pb, framePath);
        if (ok) {
          std::string subject;
          subject.append(app);
          subject.push_back('\t');
          subject.append(title);
          subject.push_back('\t');
          subject.append(fpath);
          metrics::record("ctx.frame", now, 0, true, subject);

          NSString* ocrText = capture::run_ocr(pb);
          if (ocrText.length > 0) {
            capture::insert_fts(now, app, title, [ocrText UTF8String]);
          }
        }
        CVPixelBufferRelease(pb);

        // Periodically enforce spool limit.
        static int s_counter = 0;
        if (++s_counter % 50 == 0) {
          capture::enforce_spool_limit();
        }
      }
    });
  }
}

- (void)stream:(SCStream*)stream didStopWithError:(NSError*)error {
  trace::log("error", std::string("capture: stream stopped: ") +
             (error ? [[error localizedDescription] UTF8String] : "unknown"));
  (void)stream;
  // Best-effort: restart after a delay.
  if (capture::g_running.load()) {
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC),
                   dispatch_get_main_queue(), ^{
      // Re-start capture if still running.
      if (capture::g_running.load()) {
        capture::stop();
        capture::start(capture::g_cfg);
      }
    });
  }
}

@end

namespace capture {
namespace {

static SeqCaptureOutput* g_output = nil;

void spawn_sync() {
  if (g_cfg.sync_script.empty()) return;
  struct ::stat st {};
  if (::stat(g_cfg.sync_script.c_str(), &st) != 0) return;
  std::string err;
  process::spawn({"/bin/bash", g_cfg.sync_script}, &err);
}

}  // namespace

// ---------------------------------------------------------------------------
//  Public API
// ---------------------------------------------------------------------------

void start(const Config& cfg) {
  if (g_running.exchange(true)) return;  // already running
  g_cfg = cfg;

  // Initialize FTS.
  if (!cfg.fts_db_path.empty()) {
    std::lock_guard<std::mutex> lock(g_fts_mu);
    init_fts(cfg.fts_db_path.c_str());
  }

  // Create spool dir.
  g_spool_ns = [NSString stringWithUTF8String:cfg.spool_dir.c_str()];
  [[NSFileManager defaultManager] createDirectoryAtPath:g_spool_ns
                            withIntermediateDirectories:YES
                                             attributes:nil
                                                  error:nil];

  g_cap_q = dispatch_queue_create("seq.capture.stream", DISPATCH_QUEUE_SERIAL);
  g_ocr_q = dispatch_queue_create("seq.capture.ocr",
                dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_SERIAL,
                                                        QOS_CLASS_BACKGROUND, 0));

  // ScreenCaptureKit setup (async).
  trace::log("info", "capture: requesting shareable content");
  [SCShareableContent getShareableContentExcludingDesktopWindows:NO
      onScreenWindowsOnly:NO
      completionHandler:
      ^(SCShareableContent* content, NSError* error) {
    if (error) {
      trace::log("error", std::string("capture: getShareableContent error: ") +
                 [[error localizedDescription] UTF8String]);
      return;
    }
    if (!content || content.displays.count == 0) {
      trace::log("error", "capture: no displays found");
      return;
    }
    trace::log("info", std::string("capture: found ") +
               std::to_string(content.displays.count) + " display(s)");

    SCDisplay* display = content.displays.firstObject;
    SCContentFilter* filter =
        [[SCContentFilter alloc] initWithDisplay:display excludingWindows:@[]];

    SCStreamConfiguration* config = [[SCStreamConfiguration alloc] init];
    config.width  = (size_t)(display.width / 2);
    config.height = (size_t)(display.height / 2);
    config.minimumFrameInterval = CMTimeMake(1, 2);  // 2 FPS (adaptive drops most)
    config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarFullRange;
    config.showsCursor = NO;
    config.queueDepth  = 3;

    g_output = [[SeqCaptureOutput alloc] init];
    g_stream = [[SCStream alloc] initWithFilter:filter
                                  configuration:config
                                       delegate:g_output];

    NSError* addErr = nil;
    [g_stream addStreamOutput:g_output type:SCStreamOutputTypeScreen
                sampleHandlerQueue:g_cap_q error:&addErr];
    if (addErr) {
      trace::log("error", std::string("capture: addStreamOutput error: ") +
                 [[addErr localizedDescription] UTF8String]);
      return;
    }

    trace::log("info", "capture: starting stream");
    [g_stream startCaptureWithCompletionHandler:^(NSError* startErr) {
      if (startErr) {
        trace::log("error", std::string("capture: startCapture error: ") +
                   [[startErr localizedDescription] UTF8String]);
        g_running.store(false);
      } else {
        trace::log("info", "capture: stream started successfully");
      }
    }];
  }];

  // Periodic sync to Hetzner (every 5 minutes).
  if (!cfg.sync_script.empty()) {
    g_sync_timer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0,
                       dispatch_get_global_queue(QOS_CLASS_UTILITY, 0));
    dispatch_source_set_timer(g_sync_timer,
                              dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC),
                              300 * NSEC_PER_SEC,  // every 5 min
                              30 * NSEC_PER_SEC);   // 30s leeway
    dispatch_source_set_event_handler(g_sync_timer, ^{ spawn_sync(); });
    dispatch_resume(g_sync_timer);
  }
}

void stop() {
  g_running.store(false);
  if (g_stream) {
    [g_stream stopCaptureWithCompletionHandler:^(NSError*) {}];
    g_stream = nil;
  }
  if (g_sync_timer) {
    dispatch_source_cancel(g_sync_timer);
    g_sync_timer = nil;
  }
}

std::string search(const std::string& query, int max_results) {
  std::lock_guard<std::mutex> lock(g_fts_mu);
  if (!g_fts_db) return "{\"results\":[]}";

  sqlite3_stmt* stmt = nullptr;
  const char* sql =
      "SELECT ts_ms, app_name, window_title, "
      "snippet(frame_text, 3, '[', ']', '...', 40) "
      "FROM frame_text WHERE frame_text MATCH ? ORDER BY rank LIMIT ?";
  if (sqlite3_prepare_v2(g_fts_db, sql, -1, &stmt, nullptr) != SQLITE_OK) {
    return "{\"results\":[]}";
  }
  sqlite3_bind_text(stmt, 1, query.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_int(stmt, 2, max_results > 0 ? max_results : 20);

  std::string out;
  out.reserve(1024);
  out.append("{\"results\":[");
  bool first = true;
  while (sqlite3_step(stmt) == SQLITE_ROW) {
    if (!first) out.push_back(',');
    first = false;
    const char* ts   = (const char*)sqlite3_column_text(stmt, 0);
    const char* app  = (const char*)sqlite3_column_text(stmt, 1);
    const char* ttl  = (const char*)sqlite3_column_text(stmt, 2);
    const char* snip = (const char*)sqlite3_column_text(stmt, 3);
    out.append("{\"ts_ms\":").append(ts ? ts : "0");
    out.append(",\"app\":\"").append(app ? app : "");
    out.append("\",\"title\":\"").append(ttl ? ttl : "");
    out.append("\",\"snippet\":\"").append(snip ? snip : "");
    out.append("\"}");
  }
  out.append("]}");
  sqlite3_finalize(stmt);
  return out;
}

}  // namespace capture
