cmake_minimum_required(VERSION 3.20)

# Exclude unused components that fail to compile under IDF 6's stricter -Werror
set(EXCLUDE_COMPONENTS spiffs)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp_frame)
