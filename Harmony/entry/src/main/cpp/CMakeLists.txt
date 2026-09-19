cmake_minimum_required(VERSION 3.5.0)
project(entry)

set(CMAKE_CXX_STANDARD 17)

set(NATIVERENDER_SRC_PATH ${CMAKE_CURRENT_SOURCE_DIR})

include_directories(${NATIVERENDER_SRC_PATH})

add_library(entry SHARED napi_init.cpp)

set(TAILSCALERS_A "${CMAKE_CURRENT_SOURCE_DIR}/../libs/arm64-v8a/libtailscalers_ohos.a")

target_link_libraries(entry PUBLIC
    libace_napi.z.so
    libhilog_ndk.z.so
    ${TAILSCALERS_A}
    dl pthread m c
)
