cmake_minimum_required(VERSION 3.16)
project(evcam_tools CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# OpenEB はローカルビルドツリーを使う（system install していない）。
# scripts/build_tools.sh がこの 2 つを渡す。
find_package(MetavisionSDK COMPONENTS base core stream REQUIRED)
find_package(MetavisionHAL REQUIRED)

foreach(tool latency_probe trigger_probe)
    add_executable(${tool} ${tool}.cpp)
    target_link_libraries(${tool}
        PRIVATE
            MetavisionSDK::base
            MetavisionSDK::core
            MetavisionSDK::stream
            Metavision::HAL
    )
endforeach()
