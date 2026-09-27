#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>

/* Diagnostic preload only. Worker ordinals are creation order, not stable roles. */
typedef int (*create_fn)(pthread_t*, const pthread_attr_t*, void*(*)(void*), void*);
static create_fn real_create;
static _Atomic unsigned next_thread;
static unsigned cpus[CPU_SETSIZE], count;
struct start_info { void*(*fn)(void*); void* arg; unsigned ordinal; };

static unsigned parse_cpu(const char* text) {
  char* end;
  if (!text || !*text) _exit(122);
  long cpu = strtol(text, &end, 10);
  if (*end || cpu < 0 || cpu >= CPU_SETSIZE) _exit(122);
  return (unsigned)cpu;
}
static void pin(unsigned cpu) {
  cpu_set_t mask;
  CPU_ZERO(&mask);
  CPU_SET(cpu, &mask);
  if (sched_setaffinity(0, sizeof(mask), &mask)) { perror("measurement affinity"); _exit(120); }
}
__attribute__((constructor)) static void initialize(void) {
  real_create = (create_fn)dlsym(RTLD_NEXT, "pthread_create");
  if (!real_create) _exit(121);
  const char* setting = getenv("WIRESTEAD_BENCH_WORKER_CPUS");
  if (!setting || !*setting) _exit(122);
  char* list = strdup(setting);
  if (!list) _exit(123);
  char* save;
  for (char* item = strtok_r(list, ",", &save); item; item = strtok_r(NULL, ",", &save)) {
    if (count >= CPU_SETSIZE) _exit(122);
    cpus[count++] = parse_cpu(item);
  }
  free(list);
  if (!count) _exit(122);
  pin(parse_cpu(getenv("WIRESTEAD_BENCH_MAIN_CPU")));
}
static void* start(void* ptr) {
  struct start_info info = *(struct start_info*)ptr;
  free(ptr);
  unsigned cpu = cpus[info.ordinal % count];
  pin(cpu);
  fprintf(stderr, "MEASUREMENT_PIN ordinal=%u cpu=%u\n", info.ordinal, cpu);
  return info.fn(info.arg);
}
int pthread_create(pthread_t* thread, const pthread_attr_t* attr, void*(*fn)(void*), void* arg) {
  struct start_info* info = malloc(sizeof(*info));
  if (!info) return ENOMEM;
  *info = (struct start_info){fn, arg, atomic_fetch_add(&next_thread, 1)};
  int rc = real_create(thread, attr, start, info);
  if (rc) free(info);
  return rc;
}
