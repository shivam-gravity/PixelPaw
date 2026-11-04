"""Runs obs/pixelpaw.lua against a fake `obslua` API (needs `pip install lupa`; skipped otherwise).

Checks syntax, buttons, auto-start, overlay creation, URL mode, no duplicate scene items, no leaked references.
"""
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
try:
    try:
        from lupa.luajit21 import LuaRuntime  # OBS uses LuaJIT
    except ImportError:
        from lupa import LuaRuntime
except ImportError:
    print("SKIPPED: pip install lupa to test the OBS script")
    sys.exit(0)

FAKE = r"""
calls = {}
local function log(...) local t = {...}; for i = 1, #t do t[i] = tostring(t[i]) end; table.insert(calls, table.concat(t, " ")) end
local sources, scene_items, refcount = {}, {}, 0
local function new_data() return {strings = {}, ints = {}, bools = {}} end
os.execute = function(cmd) log("exec", cmd); return true end
pixelpaw_spawn = function(file, params, dir, visible) log("exec", file .. " " .. params .. " in " .. dir); return true end
function script_path() return SCRIPT_DIR end
obslua = {
  OBS_TEXT_DEFAULT = 0, OBS_PATH_DIRECTORY = 2, LOG_INFO = 300, LOG_WARNING = 200,
  obs_properties_create = function() return {props = {}} end,
  obs_properties_add_text = function(p, k) p.props[k] = "text" end,
  obs_properties_add_bool = function(p, k) p.props[k] = "bool" end,
  obs_properties_add_path = function(p, k) p.props[k] = "path" end,
  obs_properties_add_button = function(p, k, label, cb) p.props[k] = cb end,
  obs_data_create = function() refcount = refcount + 1; return new_data() end,
  obs_data_release = function(d) refcount = refcount - 1 end,
  obs_data_set_default_string = function(d, k, v) if d.strings[k] == nil then d.strings[k] = v end end,
  obs_data_set_default_bool = function(d, k, v) if d.bools[k] == nil then d.bools[k] = v end end,
  obs_data_get_string = function(d, k) return d.strings[k] or "" end,
  obs_data_get_bool = function(d, k) return d.bools[k] == true end,
  obs_data_set_string = function(d, k, v) d.strings[k] = v end,
  obs_data_set_int = function(d, k, v) d.ints[k] = v end,
  obs_data_set_bool = function(d, k, v) d.bools[k] = v end,
  obs_video_info = function() return {} end,
  obs_get_video_info = function(ovi) ovi.base_width = 2560; ovi.base_height = 1440; return true end,
  obs_frontend_get_current_scene = function() refcount = refcount + 1; return {name = "Scene"} end,
  obs_scene_from_source = function(s) return s end,
  obs_get_source_by_name = function(n) if sources[n] then refcount = refcount + 1 end; return sources[n] end,
  obs_source_create = function(kind, name, settings, hotkeys)
    refcount = refcount + 1
    local s = {kind = kind, name = name, url = settings.strings.url, local_file = settings.bools.is_local_file,
               w = settings.ints.width, h = settings.ints.height}
    sources[name] = s; log("create", kind, name, s.url, s.w .. "x" .. s.h, "local_file=" .. tostring(s.local_file)); return s end,
  obs_source_update = function(s, settings) s.url = settings.strings.url; log("update", s.name, s.url) end,
  obs_source_release = function(s) refcount = refcount - 1 end,
  obs_scene_find_source = function(scene, name) return scene_items[name] end,
  obs_scene_add = function(scene, s) scene_items[s.name] = true; log("add_to_scene", s.name) end,
  obs_source_properties = function(s) return {refresh = true} end,
  obs_properties_get = function(p, k) if k == "refreshnocache" then return {} end end,
  obs_property_button_clicked = function(p, s) log("refresh", s.name) end,
  obs_properties_destroy = function(p) end,
  script_log = function(level, msg) log("log", msg) end,
}
function get_refcount() return refcount end
"""

lua = LuaRuntime(unpack_returned_tuples=True)
lua.globals().SCRIPT_DIR = str(ROOT / "obs") + "/"
lua.execute(FAKE)
lua.execute((ROOT / "obs" / "pixelpaw.lua").read_text(encoding="utf-8"))
g = lua.globals()


def calls():
    return [g.calls[i] for i in range(1, len(g.calls) + 1)]


assert "PixelPaw" in g.script_description()
settings = lua.eval("{strings = {}, ints = {}, bools = {}}")
g.script_defaults(settings)
assert Path(settings.strings["folder"]).resolve() == ROOT.resolve(), settings.strings["folder"]
props = g.script_properties()
for key in ("autostart", "open", "add", "reload", "url", "folder"):
    assert props.props[key], key

g.script_load(settings)  # autostart is on by default -> launches in the background
c = calls()
assert any(x.startswith("exec ") and "--no-browser" in x and "PixelPaw" in x for x in c), c
g.on_open(None, None)
assert calls()[-2].startswith("exec ") and "--no-browser" not in calls()[-2], calls()[-2:]

g.on_reload(None, None)  # before adding: warns, no crash
g.on_add(None, None)
g.on_add(None, None)     # second click updates, no duplicate scene item
g.on_reload(None, None)
c = calls()
assert any(x.startswith("create browser_source PixelPaw Overlay http://127.0.0.1:8765/overlay 2560x1440 local_file=false") for x in c), c
assert sum(x.startswith("add_to_scene") for x in c) == 1
assert any(x.startswith("refresh") for x in c)
assert g.get_refcount() == 0, f"leaked OBS references: {g.get_refcount()}"

# With the native plugin loaded, the script doesn't start PixelPaw a second time.
g.obslua.obs_get_module = lua.eval('function(name) if name == "pixelpaw" then return {} end end')
before = len(calls())
g.script_load(settings)
assert not any(x.startswith("exec ") for x in calls()[before:]), calls()[before:]
assert "plugin is installed" in calls()[-1], calls()[-1]
g.obslua.obs_get_module = None

settings.strings["folder"] = "C:/does/not/exist"
settings.bools["autostart"] = True
g.script_load(settings)
assert "not found" in calls()[-1], calls()[-1]
print("✔ OBS script: syntax, buttons, auto-start (background), Open PixelPaw, overlay in URL mode,")
print("  canvas-sized, no duplicates, no reference leaks, clear message when the folder is wrong,")
print("  no second launch when the native plugin is loaded")
print("\nOBS LUA TEST PASSED")
