cmake_minimum_required(VERSION 3.14)
project(seqch LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

include(FetchContent)

# Prefer static clickhouse-cpp so our libseqch.dylib doesn't carry unmanaged dynamic deps.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(CLICKHOUSE_CPP_BUILD_SHARED OFF CACHE BOOL "" FORCE)

FetchContent_Declare(
    clickhouse-cpp
    GIT_REPOSITORY https://github.com/ClickHouse/clickhouse-cpp.git
    GIT_TAG v2.5.1
    GIT_SHALLOW TRUE
)

# Disable OpenSSL - we connect to localhost, no TLS needed
set(WITH_OPENSSL OFF CACHE BOOL "" FORCE)
set(WITH_SYSTEM_ABSEIL OFF CACHE BOOL "" FORCE)
set(WITH_SYSTEM_LZ4 OFF CACHE BOOL "" FORCE)
set(WITH_SYSTEM_CITYHASH OFF CACHE BOOL "" FORCE)

FetchContent_MakeAvailable(clickhouse-cpp)

# Build our wrapper as a shared library so run.sh can link it easily.
# All clickhouse-cpp transitive deps are bundled inside.
add_library(seqch SHARED
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/clickhouse.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/clickhouse_bridge.cpp
)

target_include_directories(seqch PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/../src
)

target_link_libraries(seqch PRIVATE clickhouse-cpp-lib)

set_target_properties(seqch PROPERTIES
    INSTALL_RPATH "@loader_path"
    BUILD_WITH_INSTALL_RPATH TRUE
    INSTALL_NAME_DIR "@rpath"
)
