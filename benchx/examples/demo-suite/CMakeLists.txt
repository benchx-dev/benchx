cmake_minimum_required(VERSION 3.20)
project(demo CXX)

option(DEMO_HARDENED "Simulated hardening checks, the one option the environments demo changes" OFF)
set(CMAKE_CXX_STANDARD 17)

find_package(benchmark QUIET)
if(NOT benchmark_FOUND)
  include(FetchContent)
  set(BENCHMARK_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_ENABLE_GTEST_TESTS OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(benchmark
    GIT_REPOSITORY https://github.com/google/benchmark.git
    GIT_TAG v1.9.1
    GIT_SHALLOW TRUE)
  FetchContent_MakeAvailable(benchmark)
endif()

add_executable(demo-bench bench.cpp)
target_link_libraries(demo-bench benchmark::benchmark_main)
if(DEMO_HARDENED)
  target_compile_definitions(demo-bench PRIVATE DEMO_HARDENED)
endif()
