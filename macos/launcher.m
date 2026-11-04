// RouteC.app launcher.
//
// Why an app at all: macOS grants Camera permission to the "responsible" app, and every process it
// starts inherits that grant. The Route C daemon is Python + ffmpeg; on their own they have no
// stable identity to grant, which is why the old per-call ffmpeg worked from one shell and hung
// from another. This launcher gives the daemon a fixed identity (com.ekorre.routec) that Eugene
// approves once. It stays alive as the parent of the daemon so the grant applies to it.
//
// The binary never needs rebuilding when the Python code changes, so the grant survives updates.
//
//   RouteC                         start the daemon (what launchd runs)
//   RouteC --camera-status-file F  write authorized | denied | restricted | notDetermined to F
//   RouteC --request-camera-file F ask for Camera permission (shows the macOS prompt), write result
//
// Run the two --*-file forms through `open -W -g -a RouteC.app --args ...` so the app, not the
// calling shell, is the one asking.
#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;
static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static const char *auth_name(AVAuthorizationStatus s) {
    switch (s) {
        case AVAuthorizationStatusAuthorized: return "authorized";
        case AVAuthorizationStatusDenied: return "denied";
        case AVAuthorizationStatusRestricted: return "restricted";
        default: return "notDetermined";
    }
}

static AVAuthorizationStatus request_camera(void) {
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo
                             completionHandler:^(BOOL granted) { dispatch_semaphore_signal(sem); }];
    dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, (int64_t)180 * NSEC_PER_SEC));
    return [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
}

static int write_result(const char *path, const char *value) {
    FILE *f = fopen(path, "w");
    if (!f) return 1;
    fprintf(f, "%s\n", value);
    fclose(f);
    return 0;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        AVAuthorizationStatus st = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
        if (argc > 2 && strcmp(argv[1], "--camera-status-file") == 0) {
            return write_result(argv[2], auth_name(st));
        }
        if (argc > 2 && strcmp(argv[1], "--request-camera-file") == 0) {
            if (st == AVAuthorizationStatusNotDetermined) st = request_camera();
            return write_result(argv[2], auth_name(st));
        }
        if (st == AVAuthorizationStatusNotDetermined) {
            // Ask, but do not hold the daemon hostage to the prompt: it starts now, reports
            // "no frames: camera permission" until Eugene clicks Allow, then recovers by itself.
            [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL g) {}];
        }
        setenv("ROUTEC_CAMERA_AUTH", auth_name(st), 1);

        NSString *script = [NSHomeDirectory()
            stringByAppendingPathComponent:@"Library/Application Support/RouteC/start.sh"];
        char *args[] = {"/bin/sh", (char *)script.fileSystemRepresentation, NULL};
        signal(SIGTERM, forward);
        signal(SIGINT, forward);
        signal(SIGHUP, forward);
        int rc = posix_spawn(&child, "/bin/sh", NULL, NULL, args, environ);
        if (rc != 0) {
            fprintf(stderr, "RouteC: cannot start %s: %s\n", script.fileSystemRepresentation, strerror(rc));
            return 1;
        }
        int status = 0;
        while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
        }
        return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
    }
}
