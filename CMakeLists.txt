cmake_minimum_required(VERSION 3.12)

project(wirestead_benchmarks LANGUAGES CXX)

if(DEFINED UNILINK_BENCH_USE_FETCHCONTENT AND NOT DEFINED WIRESTEAD_BENCH_USE_FETCHCONTENT)
  set(WIRESTEAD_BENCH_USE_FETCHCONTENT
      "${UNILINK_BENCH_USE_FETCHCONTENT}"
      CACHE BOOL "Fetch Wirestead using FetchContent" FORCE)
endif()
option(WIRESTEAD_BENCH_USE_FETCHCONTENT "Fetch Wirestead using FetchContent" OFF)
if(DEFINED UNILINK_BENCH_USE_FETCHCONTENT
   AND NOT "${UNILINK_BENCH_USE_FETCHCONTENT}" STREQUAL "${WIRESTEAD_BENCH_USE_FETCHCONTENT}")
  message(FATAL_ERROR "UNILINK_BENCH_USE_FETCHCONTENT conflicts with WIRESTEAD_BENCH_USE_FETCHCONTENT")
endif()

option(WIRESTEAD_BENCH_REQUEST_TRACE "Build opt-in per-request diagnostic tracing" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

if(WIRESTEAD_BENCH_USE_FETCHCONTENT)
  include(cmake/FetchWirestead.cmake)
else()
  find_package(wirestead CONFIG QUIET)
  if(NOT TARGET wirestead::wirestead)
    find_package(unilink CONFIG REQUIRED)
  endif()
endif()

if(TARGET wirestead::wirestead)
  set(WIRESTEAD_BENCH_TARGET wirestead::wirestead)
elseif(TARGET unilink::unilink)
  set(WIRESTEAD_BENCH_TARGET unilink::unilink)
  add_compile_definitions(WIRESTEAD_BENCH_USE_LEGACY_UNILINK_API=1)
else()
  message(FATAL_ERROR "Neither wirestead::wirestead nor unilink::unilink is available")
endif()

set(WIRESTEAD_BENCH_SOURCE_KIND "installed-package")
set(WIRESTEAD_BENCH_SOURCE_PATH "")
set(WIRESTEAD_BENCH_REQUESTED_REF "")
set(WIRESTEAD_BENCH_COMMIT "")

if(WIRESTEAD_BENCH_USE_FETCHCONTENT)
  set(WIRESTEAD_BENCH_SOURCE_KIND "fetchcontent")
  set(WIRESTEAD_BENCH_REQUESTED_REF
      "${WIRESTEAD_BENCH_GIT_TAG}"
  )
  if(WIRESTEAD_BENCH_SOURCE_DIR)
    set(WIRESTEAD_BENCH_SOURCE_KIND "local-source")
    set(WIRESTEAD_BENCH_REQUESTED_REF "local-source")
    set(WIRESTEAD_BENCH_SOURCE_PATH
        "${WIRESTEAD_BENCH_SOURCE_DIR}"
    )
  elseif(DEFINED wirestead_SOURCE_DIR)
    set(WIRESTEAD_BENCH_SOURCE_PATH "${wirestead_SOURCE_DIR}")
  endif()
else()
  if(DEFINED wirestead_DIR)
    set(WIRESTEAD_BENCH_SOURCE_PATH "${wirestead_DIR}")
  elseif(DEFINED unilink_DIR)
    set(WIRESTEAD_BENCH_SOURCE_PATH "${unilink_DIR}")
  endif()
endif()

if(WIRESTEAD_BENCH_SOURCE_PATH)
  execute_process(
    COMMAND git -C "${WIRESTEAD_BENCH_SOURCE_PATH}" rev-parse HEAD
    OUTPUT_VARIABLE WIRESTEAD_BENCH_COMMIT
    ERROR_QUIET
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )
endif()

include(CheckCXXSourceCompiles)
find_package(Threads REQUIRED)

get_target_property(
  WIRESTEAD_BENCH_INCLUDE_DIRS
  ${WIRESTEAD_BENCH_TARGET}
  INTERFACE_INCLUDE_DIRECTORIES
)
if(NOT WIRESTEAD_BENCH_INCLUDE_DIRS)
  set(WIRESTEAD_BENCH_INCLUDE_DIRS "")
endif()
set(CMAKE_REQUIRED_INCLUDES ${WIRESTEAD_BENCH_INCLUDE_DIRS} ${CMAKE_CURRENT_SOURCE_DIR}/benchmarks)
if(TARGET unilink::unilink AND NOT TARGET wirestead::wirestead)
  set(WIRESTEAD_BENCH_STATS_PROBE_HEADER "#define WIRESTEAD_BENCH_USE_LEGACY_UNILINK_API 1\n")
endif()
check_cxx_source_compiles(
  "
  ${WIRESTEAD_BENCH_STATS_PROBE_HEADER}
  #include \"wirestead_bench_target.hpp\"
  #include <utility>

  int main() {
    using Stats = decltype(std::declval<wirestead::TcpClient&>().stats());
    (void)sizeof(decltype(std::declval<Stats&>().messages_accepted));
    (void)sizeof(decltype(std::declval<Stats&>().dropped_messages));
    return 0;
  }
  "
  WIRESTEAD_BENCH_HAS_RUNTIME_STATS
)
unset(CMAKE_REQUIRED_INCLUDES)

if(WIRESTEAD_BENCH_HAS_RUNTIME_STATS)
  set(WIRESTEAD_BENCH_RUNTIME_STATS_SUPPORTED 1)
else()
  set(WIRESTEAD_BENCH_RUNTIME_STATS_SUPPORTED 0)
endif()

file(
  WRITE "${CMAKE_BINARY_DIR}/wirestead_bench_metadata.csv"
  "key,value\n"
  "request_trace_compiled,${WIRESTEAD_BENCH_REQUEST_TRACE}\n"
  "wirestead_source_kind,${WIRESTEAD_BENCH_SOURCE_KIND}\n"
  "wirestead_requested_ref,${WIRESTEAD_BENCH_REQUESTED_REF}\n"
  "wirestead_source_path,${WIRESTEAD_BENCH_SOURCE_PATH}\n"
  "wirestead_commit,${WIRESTEAD_BENCH_COMMIT}\n"
  "unilink_source_kind,${WIRESTEAD_BENCH_SOURCE_KIND}\n"
  "unilink_requested_ref,${WIRESTEAD_BENCH_REQUESTED_REF}\n"
  "unilink_source_path,${WIRESTEAD_BENCH_SOURCE_PATH}\n"
  "unilink_commit,${WIRESTEAD_BENCH_COMMIT}\n"
  "runtime_stats_supported,${WIRESTEAD_BENCH_RUNTIME_STATS_SUPPORTED}\n"
)

add_library(wirestead_bench_common INTERFACE)
if(WIRESTEAD_BENCH_REQUEST_TRACE)
  target_compile_definitions(wirestead_bench_common INTERFACE WIRESTEAD_BENCH_REQUEST_TRACE=1)
endif()
target_include_directories(wirestead_bench_common
                           INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/benchmarks)
target_link_libraries(wirestead_bench_common INTERFACE ${WIRESTEAD_BENCH_TARGET})

function(add_wirestead_benchmark target source)
  add_executable(${target} ${source})
  target_link_libraries(${target} PRIVATE wirestead_bench_common)
endfunction()

add_wirestead_benchmark(bench_tcp_echo_server benchmarks/tcp/tcp_echo_server.cpp)
add_wirestead_benchmark(bench_tcp_latency_client benchmarks/tcp/tcp_latency_client.cpp)
add_wirestead_benchmark(bench_tcp_load_latency benchmarks/tcp/tcp_load_latency.cpp)
add_wirestead_benchmark(bench_udp_echo_server benchmarks/udp/udp_echo_server.cpp)
add_wirestead_benchmark(bench_udp_latency_client benchmarks/udp/udp_latency_client.cpp)
add_wirestead_benchmark(bench_udp_payload_smoke benchmarks/udp/udp_payload_smoke.cpp)
add_wirestead_benchmark(bench_uds_echo_server benchmarks/uds/uds_echo_server.cpp)
add_wirestead_benchmark(bench_uds_latency_client benchmarks/uds/uds_latency_client.cpp)
add_wirestead_benchmark(bench_strategy_matrix benchmarks/strategy/strategy_matrix.cpp)

add_executable(bench_raw_udp_payload_smoke benchmarks/udp/raw_udp_payload_smoke.cpp)
target_include_directories(bench_raw_udp_payload_smoke PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/benchmarks)
target_link_libraries(bench_raw_udp_payload_smoke PRIVATE Threads::Threads)
if(WIRESTEAD_BENCH_HAS_RUNTIME_STATS)
  target_compile_definitions(bench_udp_payload_smoke PRIVATE WIRESTEAD_BENCH_HAS_RUNTIME_STATS=1)
  target_compile_definitions(bench_strategy_matrix PRIVATE WIRESTEAD_BENCH_HAS_RUNTIME_STATS=1)
endif()
