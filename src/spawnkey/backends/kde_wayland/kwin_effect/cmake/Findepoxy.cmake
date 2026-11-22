find_library(epoxy_LIBRARY NAMES epoxy REQUIRED)
find_path(epoxy_INCLUDE_DIR NAMES epoxy/gl.h REQUIRED)
if(NOT TARGET epoxy::epoxy)
    add_library(epoxy::epoxy UNKNOWN IMPORTED)
    set_target_properties(epoxy::epoxy PROPERTIES
        IMPORTED_LOCATION "${epoxy_LIBRARY}"
        INTERFACE_INCLUDE_DIRECTORIES "${epoxy_INCLUDE_DIR}"
    )
endif()
set(epoxy_FOUND TRUE)
