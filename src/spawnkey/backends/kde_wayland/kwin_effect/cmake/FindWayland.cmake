find_library(Wayland_Server_LIBRARY NAMES wayland-server REQUIRED)
find_path(Wayland_Server_INCLUDE_DIR NAMES wayland-server.h REQUIRED)
if(NOT TARGET Wayland::Server)
    add_library(Wayland::Server UNKNOWN IMPORTED)
    set_target_properties(Wayland::Server PROPERTIES
        IMPORTED_LOCATION "${Wayland_Server_LIBRARY}"
        INTERFACE_INCLUDE_DIRECTORIES "${Wayland_Server_INCLUDE_DIR}"
    )
endif()
set(Wayland_Server_FOUND TRUE)
set(Wayland_FOUND TRUE)
