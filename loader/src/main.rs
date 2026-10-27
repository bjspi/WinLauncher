//! Keep Python, Qt and the tray icon in WinLauncher's own process image.
//!
//! Installation writes the interpreter, DLL and package directory as UTF-16
//! Windows resources. The executable itself has no Python-version dependency.

#![windows_subsystem = "windows"]

use std::ffi::{c_void, OsStr, OsString};
use std::io;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::PathBuf;
use std::ptr::null_mut;

const RT_RCDATA: usize = 10;
const LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR: u32 = 0x100;
const LOAD_LIBRARY_SEARCH_DEFAULT_DIRS: u32 = 0x1000;
const MB_ICONERROR: u32 = 0x10;

#[link(name = "kernel32")]
extern "system" {
    fn FindResourceW(module: *mut c_void, name: *const u16, kind: *const u16) -> *mut c_void;
    fn LoadResource(module: *mut c_void, resource: *mut c_void) -> *mut c_void;
    fn LockResource(resource: *mut c_void) -> *const u8;
    fn SizeofResource(module: *mut c_void, resource: *mut c_void) -> u32;
    fn SetDefaultDllDirectories(flags: u32) -> i32;
    fn AddDllDirectory(path: *const u16) -> *mut c_void;
    fn LoadLibraryExW(path: *const u16, file: *mut c_void, flags: u32) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const u8) -> *const c_void;
}

#[link(name = "user32")]
extern "system" {
    fn MessageBoxW(window: *mut c_void, text: *const u16, title: *const u16, flags: u32) -> i32;
}

fn wide(text: &OsStr) -> Vec<u16> {
    text.encode_wide().chain(Some(0)).collect()
}

fn resource(name: &str) -> Result<OsString, String> {
    let name_wide = wide(OsStr::new(name));
    // These resources belong to this executable; no external DLL is executed.
    unsafe {
        let found = FindResourceW(null_mut(), name_wide.as_ptr(), RT_RCDATA as *const u16);
        if found.is_null() {
            return Err(format!(
                "Missing {name}. Start WinLauncher through Python to repair its launcher."
            ));
        }
        let size = SizeofResource(null_mut(), found) as usize;
        let data = LockResource(LoadResource(null_mut(), found));
        if data.is_null() || size < 2 || !size.is_multiple_of(2) {
            return Err(format!("Invalid launcher resource: {name}"));
        }
        let units = std::slice::from_raw_parts(data.cast::<u16>(), size / 2);
        let text = units.strip_suffix(&[0]).unwrap_or(units);
        if text.is_empty() || text.contains(&0) {
            return Err(format!("Invalid launcher resource: {name}"));
        }
        Ok(OsString::from_wide(text))
    }
}

fn run() -> Result<i32, String> {
    let python = PathBuf::from(resource("PYTHON_EXECUTABLE")?);
    let library = PathBuf::from(resource("PYTHON_LIBRARY")?);
    let package = PathBuf::from(resource("PYTHON_PACKAGE")?);
    let entry_module = resource("PYTHON_MODULE")?;
    if !python.is_file() || !library.is_file() || !package.is_dir() {
        return Err("WinLauncher's Python installation has moved or was removed. Start it through Python to repair its launcher.".into());
    }
    std::env::set_current_dir(&package).map_err(|error| error.to_string())?;
    // CPython consumes this during path initialization, preserving the real venv
    // interpreter for pip and Python subprocesses rather than the branded host.
    std::env::set_var("__PYVENV_LAUNCHER__", &python);
    let directory = wide(
        library
            .parent()
            .ok_or("Python DLL has no directory")?
            .as_os_str(),
    );
    let library = wide(library.as_os_str());
    unsafe {
        if SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS) == 0
            || AddDllDirectory(directory.as_ptr()).is_null()
        {
            return Err(format!(
                "Cannot configure Python's DLL directory: {}",
                io::Error::last_os_error()
            ));
        }
        let module = LoadLibraryExW(
            library.as_ptr(),
            null_mut(),
            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS,
        );
        if module.is_null() {
            return Err(format!(
                "Cannot load Python: {}",
                io::Error::last_os_error()
            ));
        }
        let address = GetProcAddress(module, c"Py_Main".as_ptr().cast());
        if address.is_null() {
            return Err("The Python DLL does not export Py_Main.".into());
        }
        let py_main: unsafe extern "C" fn(i32, *mut *mut u16) -> i32 = std::mem::transmute(address);
        let arguments = [python.into_os_string(), "-m".into(), entry_module]
            .into_iter()
            .chain(std::env::args_os().skip(1));
        let mut strings: Vec<Vec<u16>> = arguments.map(|text| wide(&text)).collect();
        let mut pointers: Vec<*mut u16> =
            strings.iter_mut().map(|text| text.as_mut_ptr()).collect();
        // Python and its extension modules remain loaded until process exit.
        Ok(py_main(pointers.len() as i32, pointers.as_mut_ptr()))
    }
}

fn main() {
    let result = match run() {
        Ok(code) => code,
        Err(error) => {
            let text = wide(OsStr::new(&error));
            let title = wide(OsStr::new("WinLauncher cannot start"));
            unsafe { MessageBoxW(null_mut(), text.as_ptr(), title.as_ptr(), MB_ICONERROR) };
            1
        }
    };
    std::process::exit(result);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_arguments_preserve_unicode_and_spaces() {
        let original = OsStr::new("C:\\Grüße 日本語\\a folder\\");
        let encoded = wide(original);
        assert_eq!(encoded.last(), Some(&0));
        assert_eq!(OsString::from_wide(&encoded[..encoded.len() - 1]), original);
    }
}
