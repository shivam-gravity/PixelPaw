cmake_minimum_required(VERSION 3.22)
project(pixelpaw-obs VERSION 1.0.0 LANGUAGES CXX)

# Two ways to build:
#  * Windows quick build (build.ps1): PIXELPAW_OBS_SDK points at headers + import libs made from your OBS install.
#  * Full OBS dev setup (any OS): libobs and obs-frontend-api CMake packages are found the usual way.
set(PIXELPAW_OBS_SDK "" CACHE PATH "Folder with include/ and lib/ for libobs, obs-frontend-api and obs-browser's panel header")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC OFF) # no Q_OBJECT classes of our own
set(CMAKE_EXPORT_COMPILE_COMMANDS ON) # lets editors (clangd) find the OBS/Qt headers

find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets Network)

add_library(pixelpaw MODULE src/plugin.cpp)
target_link_libraries(pixelpaw PRIVATE Qt6::Core Qt6::Gui Qt6::Widgets Qt6::Network)

if(PIXELPAW_OBS_SDK)
  target_include_directories(pixelpaw PRIVATE
    "${PIXELPAW_OBS_SDK}/include/libobs"
    "${PIXELPAW_OBS_SDK}/include/frontend-api"
    "${PIXELPAW_OBS_SDK}/include/browser")
  target_link_libraries(pixelpaw PRIVATE
    "${PIXELPAW_OBS_SDK}/lib/obs${CMAKE_STATIC_LIBRARY_SUFFIX}"
    "${PIXELPAW_OBS_SDK}/lib/obs-frontend-api${CMAKE_STATIC_LIBRARY_SUFFIX}")
else()
  find_package(libobs REQUIRED)
  find_package(obs-frontend-api REQUIRED)
  target_link_libraries(pixelpaw PRIVATE OBS::libobs OBS::obs-frontend-api)
  # browser-panel.hpp lives in the obs-browser plugin sources (plugins/obs-browser/panel)
  set(OBS_BROWSER_PANEL_DIR "" CACHE PATH "obs-browser/panel folder")
  target_include_directories(pixelpaw PRIVATE "${OBS_BROWSER_PANEL_DIR}")
endif()

set_target_properties(pixelpaw PROPERTIES PREFIX "")
if(MSVC)
  target_compile_options(pixelpaw PRIVATE /utf-8 /W3 /wd4996)
endif()
