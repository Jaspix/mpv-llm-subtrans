local utils = require 'mp.utils'
local msg = require 'mp.msg'

local options = {
    dest_lang = "", -- the language you want, default to guess with system's language
    api_key = "", -- default to read from environment variable OPENAI_API_KEY
    model = "",  -- default to use default model (gpt-4o-mini or deepseek-chat)
    base_url = "", -- default to guess from key (OpenAI or DeekSeek)
    python_bin = "", -- path to python or uv, default to find `uv`, `python3` & `py` from PATH
    ffmpeg_bin = "ffmpeg", -- path to ffmpeg execute
    batch_size = 50, -- number of dialogous send in one translate request
    output_dir = "", -- where to put translated srt files, empty = save next to the video file
    extra_prompt = "", -- append to developer prompt
    skip_env_check = false, -- fast start, skip prerequisites checking
    pre_translate_seconds = 300, -- how far ahead to translate in progressive mode
    advance_threshold_seconds = 60, -- trigger next chunk when this close to running out
    continuous_mode = false, -- if true, translate chunks continuously in background without waiting for playback
    chunk_by_batch = true, -- if true, chunks match batch_size lines instead of fixed time duration
    reasoning_effort = "none", -- reasoning effort for models: none, low, medium, high
    osd_font_size = 20, -- OSD status and progress message font size (default: 20)
}

local ASS_COLOR_RED = "{\\c&H8899FF&}"
local ASS_COLOR_GREEN = "{\\c&H99FF88&}"
local IS_WINDODWS = mp.get_property("vo-mmcss-profile") ~= nil  -- Windows only property

local created_chunk_dirs = {}  -- chunk dirs created this mpv session, cleaned on shutdown
local created_temp_files = {}  -- temp files created this mpv session, cleaned on shutdown

local function format_time(sec)
    if sec == nil or sec < 0 then sec = 0 end
    local m = math.floor(sec / 60)
    local s = math.floor(sec % 60)
    return string.format("%d:%02d", m, s)
end

local function cleanup_chunk_dir(chunk_dir)
    if chunk_dir == nil then return end
    local entries = utils.readdir(chunk_dir)
    if entries ~= nil then
        for _, fname in ipairs(entries) do
            os.remove(utils.join_path(chunk_dir, fname))
        end
    end
    -- os.remove can't remove directories on Windows
    if IS_WINDODWS then
        os.execute("rmdir /s /q \"" .. chunk_dir .. "\"")
    else
        os.remove(chunk_dir)
    end
end

--- Check python (python, py, or uv) version
-- @return boolean, "python" | "uv" | error_string
local function check_python_version(bin)
    local ret = mp.command_native({
        name="subprocess",
        args={bin, "-V"},
        playback_only=false,
        capture_stdout=true,
    })
    if ret.status ~= 0 then
        if ret.error_string == "init" then
            return false, "cannot execute " .. bin
        else
            return false, bin .. " exit with error code " .. ret.status
        end
    end
    local name, ver1, ver2 = ret.stdout:match("^(%w+) (%d+)%.(%d+)")
    if name == "uv" then
        -- do not check version of uv
        return true, "uv"
    end
    if ver1 ~= "3" or tonumber(ver2) < 8 then
        return false, "Python version " .. ver1 .. "." .. ver2 .. " not supported"
    end
    return true, "python"
end

local function check_python_openai(python_bin)
    local ret = mp.command_native({
        name="subprocess",
        args={python_bin, "-c", "import openai; print(openai.__version__)"},
        playback_only=false,
        capture_stdout=true,
    })
    if ret.status ~= 0 then
        msg.warn(python_bin, "-c import openai:", ret.status)
        return false
    end
    msg.info("openai found:", ret.stdout:gsub("%s+$", ""))
    return true
end

local function check_ffmpeg(bin)
    local ret = mp.command_native({
        name="subprocess",
        args={bin, "-version"},
        playback_only=false,
        capture_stdout=true,
    })
    if ret.status ~= 0 then
        msg.warn(bin, "exit with", ret.status)
        return false
    end
    msg.info("ffmpeg found:", ret.stdout:match("^([^-]+)"))
    return true
end

-- The API key is passed to subtrans.py as a command-line argument instead of
-- mpv's subprocess `env` argument: on Windows, `env` replaces the whole child
-- environment (dropping PATH/TEMP/...) and can make process creation fail with
-- "init", while `stdin_data` is unreliable on Windows. Without `env`, the child
-- inherits the full environment of mpv.

--- Find compatible python (or uv) execute
-- @param candidates arrays of candidates, or nil
-- @return (path, "python" | "uv") | nil
local function find_python_bin(candidates)
    if candidates == nil and IS_WINDODWS then
        candidates = {"uv", "py", "python"}
    elseif candidates == nil then
        candidates = {"uv", "python3", "python"}
    end
    for _, bin in ipairs(candidates) do
        local ok, py_type = check_python_version(bin)
        if ok then
            msg.info("Python found as", bin)
            return bin, py_type
        end
    end
    return nil
end

--- Find & build python exec args list
--@return (string array, "python" | "uv") | nil
local function get_python_exec_args()
    -- get python execute path & type
    local python_bin = options.python_bin
    local py_type
    if options.skip_env_check then
        if python_bin == "" then
            -- use uv by default
            python_bin = "uv"
        end
        -- guess type by name
        if python_bin:match("[\\/]?uv[^\\/]*$") == nil then
            py_type = "uv"
        else
            py_type = "python"
        end
    else
        -- find python bin
        local candidates = nil
        if python_bin ~= "" then
            candidates = {python_bin}
        end
        local bin, type = find_python_bin(candidates)
        if bin == nil or type == nil then
            return nil
        end
        python_bin, py_type = bin, type
    end

    -- build args
    if py_type == "python" then
        return {python_bin, "-u"}, py_type
    elseif py_type == "uv" then
        return {python_bin, "run",}, py_type
    else
        return {python_bin}, py_type
    end
end

--- Create OSD overlay with show/remove helpers
-- @return ov, show(msg), remove_ov(delay_secs)
local function create_osd()
    local ov = mp.create_osd_overlay("ass-events")
    local function show(msg)
        local fs = options.osd_font_size or 20
        ov.data = string.format("{\\b1}{\\fs%d}LLM SubTrans{\\b0} - %s", fs, msg)
        ov:update()
    end
    local function remove_ov(delay_secs)
        if delay_secs == nil or delay_secs == 0 then
            ov:remove()
        else
            mp.add_timeout(delay_secs, function()
                ov:remove()
            end)
        end
    end
    return ov, show, remove_ov
end

--- Build a sequential text progress bar
-- @param pct number (0 - 100)
-- @param width integer (default 16)
-- @return string formatted progress bar
local function make_progress_bar(pct, width)
    width = width or 16
    local filled = math.floor((pct / 100) * width + 0.5)
    if filled > width then filled = width end
    if filled < 0 then filled = 0 end
    local empty = width - filled
    return string.rep("█", filled) .. string.rep("░", empty)
end

--- Check python, openai module and ffmpeg availability
-- @return boolean, error_msg
local function check_environment(py_args, py_type)
    -- check python-openai
    if not options.skip_env_check and py_type == "python" then
        local ok, _ = check_python_openai(py_args[1])
        if not ok then
            return false, "Python module `openai` not found"
        end
    end

    -- check ffmpeg
    if not options.skip_env_check then
        if not check_ffmpeg(options.ffmpeg_bin) then
            return false, "`ffmpeg` not found"
        end
    end
    return true, nil
end

--- Select current or first subtitle track
-- @return sub_track | nil
local function select_subtitle_track()
    local sub_track = mp.get_property_native("current-tracks/sub")
    if sub_track == nil then
        -- find first subtitle track
        local tracks = mp.get_property_native("track-list")
        for _, track in ipairs(tracks) do
            if track.type == "sub" then
                sub_track = track
                break
            end
        end
    end
    return sub_track
end

--- Get video URL and external subtitle URL, validate external subtitles
-- @return video_url, ext_sub_url, error_msg
local function get_video_url(sub_track)
    local ext_sub_url = ""
    if sub_track["external"] then
        ext_sub_url = sub_track["external-filename"]
        msg.info("External subtitle " .. ext_sub_url)
        if not ext_sub_url:match("%.srt$") then
            -- TODO: support ass subtitle?
            return nil, nil, "only support SubRip (.srt) for external subtitles"
        end
        if ext_sub_url:match("^https?://") then
            -- TODO: support http subtitle?
            return nil, nil, "only support external subtitles from local file"
        end
    end

    -- TODO: check video url protocol
    local video_url = mp.get_property("path")
    return video_url, ext_sub_url, nil
end

local LANG_NAME_TO_ISO = {
    ["spanish"] = "es",
    ["español"] = "es",
    ["espanol"] = "es",
    ["english"] = "en",
    ["ingles"] = "en",
    ["inglés"] = "en",
    ["japanese"] = "ja",
    ["japonés"] = "ja",
    ["japones"] = "ja",
    ["chinese"] = "zh",
    ["chino"] = "zh",
    ["simplified chinese"] = "zh-Hans",
    ["traditional chinese"] = "zh-Hant",
    ["french"] = "fr",
    ["francés"] = "fr",
    ["frances"] = "fr",
    ["german"] = "de",
    ["alemán"] = "de",
    ["aleman"] = "de",
    ["portuguese"] = "pt",
    ["portugués"] = "pt",
    ["portugues"] = "pt",
    ["brazilian portuguese"] = "pt-BR",
    ["italian"] = "it",
    ["italiano"] = "it",
    ["korean"] = "ko",
    ["coreano"] = "ko",
    ["russian"] = "ru",
    ["ruso"] = "ru",
    ["arabic"] = "ar",
    ["árabe"] = "ar",
    ["arabe"] = "ar",
    ["hindi"] = "hi",
    ["turkish"] = "tr",
    ["turco"] = "tr",
    ["vietnamese"] = "vi",
    ["vietnamita"] = "vi",
    ["polish"] = "pl",
    ["polaco"] = "pl",
    ["dutch"] = "nl",
    ["holandés"] = "nl",
    ["holandes"] = "nl",
    ["indonesian"] = "id",
    ["indonesio"] = "id",
    ["ukrainian"] = "uk",
    ["ucraniano"] = "uk",
    ["swedish"] = "sv",
    ["sueco"] = "sv",
    ["norwegian"] = "no",
    ["noruego"] = "no",
    ["danish"] = "da",
    ["danés"] = "da",
    ["danes"] = "da",
    ["finnish"] = "fi",
    ["finlandés"] = "fi",
    ["finlandes"] = "fi",
    ["greek"] = "el",
    ["griego"] = "el",
    ["czech"] = "cs",
    ["checo"] = "cs",
    ["thai"] = "th",
    ["tailandés"] = "th",
    ["tailandes"] = "th",
    ["latin american spanish"] = "es-419",
    ["spanish (latin america)"] = "es-419",
    ["español latino"] = "es-419",
    ["espanol latino"] = "es-419",
}

local function get_iso_lang_code(dest_lang)
    if dest_lang == nil or dest_lang == "" then
        return "trans"
    end
    local trimmed = dest_lang:match("^%s*(.-)%s*$"):lower()
    if LANG_NAME_TO_ISO[trimmed] then
        return LANG_NAME_TO_ISO[trimmed]:lower()
    end
    local iso_match = dest_lang:match("^([%a%d%-_]+)$")
    if iso_match then
        return iso_match:lower()
    end
    local clean = dest_lang:gsub("[^%a%d%-_]", "")
    if clean ~= "" then
        return clean:lower()
    end
    return "trans"
end

--- Resolve output directory and srt file path
-- @param ext_sub_url external subtitle path (if any)
-- @return output_dir, srt_path, lang_code
local function resolve_output_path(ext_sub_url)
    local output_dir
    if options.output_dir == "" then
        -- default: save next to the currently playing video file
        local video_dir = utils.split_path(mp.get_property("path"))
        if video_dir == nil or video_dir == "" then
            -- fallback when no directory can be determined (e.g. URL)
            local desktop_dir = os.getenv("USERPROFILE") or os.getenv("HOME")
            if desktop_dir ~= nil and desktop_dir ~= "" then
                output_dir = utils.join_path(desktop_dir, "Desktop")
            else
                output_dir = mp.get_script_directory()
            end
        else
            output_dir = video_dir
        end
        output_dir = mp.command_native({"expand-path", output_dir})
    else
        output_dir = mp.command_native({"expand-path", options.output_dir})
    end

    local lang_code = get_iso_lang_code(options.dest_lang)
    local video_basename = mp.get_property("filename/no-ext")
    local srt_filename = video_basename .. "." .. lang_code .. ".srt"
    local srt_path = utils.join_path(output_dir, srt_filename)

    -- CRITICAL SAFETY CHECK: Never overwrite the source external subtitle file!
    if ext_sub_url ~= nil and ext_sub_url ~= "" then
        local normalized_ext = mp.command_native({"expand-path", ext_sub_url})
        if srt_path == normalized_ext or srt_path == ext_sub_url then
            srt_filename = video_basename .. "." .. lang_code .. ".translated.srt"
            srt_path = utils.join_path(output_dir, srt_filename)
        end
    end

    return output_dir, srt_path, lang_code
end

--- Read {panic: "msg"} from ipc file
-- @return string | nil
local function read_panic_msg(ipc_path)
    local ipc = io.open(ipc_path, "r")
    if ipc == nil then return nil end
    local state = utils.parse_json(ipc:read("*a"))
    if state == nil then return nil end
    return state["panic"]
end

--- Build full python args list (without mutating py_args)
-- @param py_args python executable args
-- @param py_script path to subtrans.py
-- @param opts table: video_url, ext_sub_url, sub_track, output_path, ipc_path
-- @param extra_args optional array of extra args to append
-- @return args array
local function build_py_args(py_args, py_script, opts, extra_args)
    local args = {}
    for _, v in ipairs(py_args) do
        table.insert(args, v)
    end
    table.insert(args, py_script)
    for _, v in ipairs({
        "--api-key", options.api_key or "",
        "--model", options.model or "",
        "--base-url", options.base_url or "",
        "--ffmpeg-bin", options.ffmpeg_bin or "ffmpeg",
        "--video-url", opts.video_url or "",
        "--subtitle-url", opts.ext_sub_url or "",
        "--sub-track-id", (opts.sub_track and opts.sub_track.id and (opts.sub_track.id - 1 .. "")) or "0",
        "--batch-size", tostring(options.batch_size or 50),
        "--dest-lang", options.dest_lang or "",
        "--extra-prompt", options.extra_prompt or "",
        "--output-path", opts.output_path or "",
        "--ipc-path", opts.ipc_path or "",
        "--reasoning-effort", options.reasoning_effort or "none",
    }) do
        table.insert(args, v)
    end
    if extra_args ~= nil then
        for _, v in ipairs(extra_args) do
            table.insert(args, v)
        end
    end
    return args
end

--- Hide the API key when logging args to debug output
local function redact_args(args)
    local out = {}
    local i = 1
    while i <= #args do
        if args[i] == "--api-key" then
            table.insert(out, "--api-key")
            table.insert(out, "***")
            i = i + 2
        else
            table.insert(out, args[i])
            i = i + 1
        end
    end
    return out
end

local running = false
local py_handle = nil
local session = nil  -- {chunk_index, translated_end_sec, chunk_files, chunk_dir, output_dir, srt_path, sub_track}
local chunk_py_handle = nil
local chunk_timer = nil
local chunk_ov = nil

local function do_full_translate()
    -- check running
    if running then
        if py_handle ~= nil then
            msg.info("kill python script (user request)")
            mp.abort_async_command(py_handle)
        else
            msg.info("already running")
        end
        return
    end

    -- Cancel active progressive session if any
    if session ~= nil then
        msg.info("Cancelling active progressive session before full translation")
        if chunk_py_handle ~= nil then
            mp.abort_async_command(chunk_py_handle)
            chunk_py_handle = nil
        end
        if chunk_timer ~= nil then
            chunk_timer:kill()
            chunk_timer = nil
        end
        if chunk_ov ~= nil then
            chunk_ov:remove()
            chunk_ov = nil
        end
        session = nil
    end

    msg.info("Start subtitle translate")
    running = true

    -- show osd
    local _, show, remove_ov = create_osd()
    show("checking")

    -- function to reset state
    local timer = nil
    local rpc_file = nil
    local function abort(error)
        if error ~= nil then
            msg.warn("Translate abort:", error)
            show(ASS_COLOR_RED .. error)
            remove_ov(5)
        else
            remove_ov(3)
        end
        running = false
        py_handle = nil
        if timer ~= nil then
            timer:kill()
            timer = nil
        end
        if rpc_file ~= nil then
            rpc_file:close()
        end
    end

    -- check python
    local py_args, py_type = get_python_exec_args()
    if py_args == nil or py_type == nil then
        return abort("Python not found")
    end

    -- check python-openai & ffmpeg
    local env_ok, env_err = check_environment(py_args, py_type)
    if not env_ok then
        return abort(env_err)
    end

    -- select subtitle track
    local sub_track = select_subtitle_track()
    if sub_track == nil then
        return abort("no source subtitle found")
    end
    msg.info("Select subtitle track#" .. sub_track.id, sub_track.title)

    -- gather metadata
    local video_url, ext_sub_url, url_err = get_video_url(sub_track)
    if url_err ~= nil then
        return abort(url_err)
    end

    -- set file path
    show("initializing")
    local output_dir, srt_path, lang_code = resolve_output_path(ext_sub_url)
    msg.info("Save file to", srt_path)

    -- set ipc file
    local ipc_path = utils.join_path(output_dir, ".progress")
    table.insert(created_temp_files, ipc_path)
    os.remove(ipc_path)

    -- API key is passed as a CLI arg; the subprocess inherits the full environment

    -- execute subtrans.py
    local script_dir = mp.get_script_directory()
    if script_dir == nil then
        return abort("script not install as directory")
    end
    local py_script = utils.join_path(script_dir, "subtrans.py")
    local tail_args = build_py_args(py_args, py_script, {
        video_url=video_url,
        ext_sub_url=ext_sub_url,
        sub_track=sub_track,
        output_path=srt_path,
        ipc_path=ipc_path,
    })
    msg.debug("Execute", utils.format_json(redact_args(tail_args)))
    py_handle = mp.command_native_async({
        name="subprocess",
        args=tail_args,
        playback_only=false,
    }, function (success, result, error)
        msg.debug("Python script exit:", utils.format_json(result))
        if not success then
            return abort("failed to execute command: " .. error)
        end
        if result.killed_by_us then
            show(ASS_COLOR_RED .. "cancelled")
            return abort()
        end
        if result.status ~= 0 then
            local panic = read_panic_msg(ipc_path)
            local log_path = utils.join_path(output_dir, "llm_subtrans_error.log")
            msg.error("LLM SubTrans script exit with error:", result.status)
            msg.info("Detailed log written to:", log_path)
            if panic ~= nil then
                return abort(panic .. " (see llm_subtrans_error.log)")
            else
                return abort("script exit with " .. result.status .. " (see llm_subtrans_error.log)")
            end
        end
        mp.command_native({name="sub-reload"})
        show(ASS_COLOR_GREEN .. "all done")
        abort()
    end)

    -- monitor output file
    local CHECK_INTERVAL_SECS = 3
    local last_progress = nil
    timer = mp.add_periodic_timer(CHECK_INTERVAL_SECS, function ()
        -- open rpc file
        if rpc_file == nil then
            rpc_file = io.open(ipc_path, "r")
            if rpc_file == nil then return end
            show("waiting")
        end
        -- read progress from rpc file
        rpc_file:seek("set")
        local progress = utils.parse_json(rpc_file:read("*a"))
        if progress == nil then return end -- ignore parse error
        -- check if progress got updated
        if last_progress ~= nil and
            last_progress["last_seq"] >= progress["last_seq"]
        then return end
        msg.info("Progress: " .. utils.format_json(progress))

        -- set/reload subtitle
        if last_progress == nil then
            -- first update, active substitles now
            msg.info("Set translated subtitles")
            mp.command_native({
                name="sub-add",
                url=srt_path,
                title="Translated [" .. lang_code .. "]",
            })
            last_progress = progress
        else
            -- only reload when necessary
            local old_sub_end_pos = last_progress["last_timestamp_millis"][2]
            local new_sub_start_pos = progress["last_timestamp_millis"][1]
            local pos = mp.get_property_native("time-pos", 0) * 1000
            -- condition 1/2: run out of dialogous
            if old_sub_end_pos - pos < CHECK_INTERVAL_SECS * 2 * 1000 then
                -- condition 2/2: new file coverd current play position
                if new_sub_start_pos > pos then
                    msg.info("Reload translated subtitles")
                    mp.command_native({name="sub-reload"})
                end
            end
            last_progress = progress
        end

        -- update progress
        local model_name = options.model ~= "" and options.model or "default"
        local short_model = model_name:match("[^/]+$") or model_name
        local total_sec = mp.get_property_native("duration/full", nil)
        local pos_sec = progress["last_timestamp_millis"][2] / 1000
        local lines_info = progress["lines_done"] and string.format(" (%d lines)", progress["lines_done"]) or ""
        if progress["status"] == "rate_limited" then
            show(string.format("{\\c&H00FFFF&}Rate limit (429) - retry in %ds (%d/%d)", progress["retry_in"] or 3, progress["attempt"] or 1, progress["max_retries"] or 3))
        elseif progress["status"] == "network_retry" then
            show(string.format("{\\c&H00FFFF&}Connection/API drop - retry in %ds (%d/%d)", progress["retry_in"] or 2, progress["attempt"] or 1, progress["max_retries"] or 3))
        elseif total_sec == nil or total_sec <= 0 then
            show(string.format("[%s] translating %s%s", short_model, format_time(pos_sec), lines_info))
        elseif pos_sec >= total_sec then
            local bar = make_progress_bar(100, 16)
            show(string.format("[%s] %s 100%%%s", short_model, bar, lines_info))
        else
            local pct = math.min(100, math.floor(pos_sec / total_sec * 100))
            local bar = make_progress_bar(pct, 16)
            show(string.format("[%s] %s %d%% (%s / %s)%s", short_model, bar, pct, format_time(pos_sec), format_time(total_sec), lines_info))
        end
    end)
end

function llm_subtrans_translate()
    local ok, err = pcall(do_full_translate)
    if not ok then
        msg.error("llm_subtrans_translate fatal error: " .. tostring(err))
        running = false
        py_handle = nil
        local _, show, remove_ov = create_osd()
        show(ASS_COLOR_RED .. "Error: " .. tostring(err))
        remove_ov(6)
    end
end

local function do_progressive_translate()
    -- If a progressive session is active, cancel it
    if session ~= nil then
        if chunk_py_handle ~= nil then
            msg.info("kill python script (user request)")
            mp.abort_async_command(chunk_py_handle)
        end
        if chunk_timer ~= nil then
            chunk_timer:kill()
            chunk_timer = nil
        end
        if chunk_ov ~= nil then
            chunk_ov:remove()
            chunk_ov = nil
        end
        -- Reload original subtitles
        mp.command_native({name="sub-reload"})
        session = nil
        chunk_py_handle = nil
        msg.info("Progressive translation cancelled")
        return
    end

    -- If a full translation is running, cancel it
    if running then
        msg.info("Cancelling active full translation before progressive translation")
        if py_handle ~= nil then
            mp.abort_async_command(py_handle)
            py_handle = nil
        end
        running = false
    end

    msg.info("Start progressive subtitle translation")
    session = {}

    -- OSD overlay
    local ov, show, remove_ov = create_osd()
    chunk_ov = ov

    -- Abort helper
    local function abort_session(error_msg)
        if error_msg ~= nil then
            msg.warn("Progressive translate abort:", error_msg)
            local log_path = session and session.output_dir and utils.join_path(session.output_dir, "llm_subtrans_error.log")
            if log_path then
                msg.info("Detailed log written to:", log_path)
            end
            show(ASS_COLOR_RED .. error_msg)
            remove_ov(6)
        else
            remove_ov(3)
        end
        session = nil
        if chunk_py_handle ~= nil then
            mp.abort_async_command(chunk_py_handle)
            chunk_py_handle = nil
        end
        if chunk_timer ~= nil then
            chunk_timer:kill()
            chunk_timer = nil
        end
        chunk_ov = nil
    end

    show("checking")

    -- Check python
    local py_args, py_type = get_python_exec_args()
    if py_args == nil or py_type == nil then
        return abort_session("Python not found")
    end

    -- Check python-openai & ffmpeg
    local env_ok, env_err = check_environment(py_args, py_type)
    if not env_ok then
        return abort_session(env_err)
    end

    -- Select subtitle track
    local sub_track = select_subtitle_track()
    if sub_track == nil then
        return abort_session("no source subtitle found")
    end
    msg.info("Select subtitle track#" .. sub_track.id, sub_track.title)
    session.sub_track = sub_track

    -- Gather metadata
    local video_url, ext_sub_url, url_err = get_video_url(sub_track)
    if url_err ~= nil then
        return abort_session(url_err)
    end

    -- Set output path
    show("initializing")
    local output_dir, srt_path, lang_code = resolve_output_path(ext_sub_url)
    msg.info("Save file to", srt_path)
    session.output_dir = output_dir
    session.srt_path = srt_path
    session.lang_code = lang_code

    -- Set up chunk directory
    local chunk_dir = utils.join_path(output_dir, ".subtrans_chunks")
    table.insert(created_chunk_dirs, chunk_dir)
    -- Clean up old chunks
    local old_chunks = utils.readdir(chunk_dir)
    if old_chunks ~= nil then
        for _, fname in ipairs(old_chunks) do
            if fname:match("^chunk_%d+%.srt$") or fname:match("^chunk_%d+%.progress$") then
                os.remove(utils.join_path(chunk_dir, fname))
            end
        end
    end
    session.chunk_dir = chunk_dir
    session.chunk_index = 0
    session.chunk_files = {}
    session.sub_added = false  -- translated subtitle track added to mpv
    session.last_translated_seq = 0  -- for precise chunk boundary skipping

    -- API key is passed as a CLI arg; the subprocess inherits the full environment

    -- Determine start position from current playback
    local start_pos_sec = mp.get_property_native("time-pos", 0)
    if start_pos_sec == nil then
        start_pos_sec = 0
    end
    if options.continuous_mode then
        -- In continuous mode, always translate from the beginning (0:00) so the whole video is covered gaplessly
        start_pos_sec = 0
    end
    session.translated_end_sec = start_pos_sec
    msg.info("Progressive translate from " .. format_time(start_pos_sec))

    -- Script directory
    local script_dir = mp.get_script_directory()
    if script_dir == nil then
        return abort_session("script not installed as directory")
    end
    local py_script = utils.join_path(script_dir, "subtrans.py")

    -- Helper: start a chunk translation
    local ipc_read_timer = nil

    local function get_chunk_srt_stats(path)
        local f = io.open(path, "r")
        if f == nil then return nil, nil end
        local max_seq = nil
        local max_end_sec = nil
        for line in f:lines() do
            local num = line:match("^%s*(%d+)%s*$")
            if num ~= nil then
                local n = tonumber(num)
                if n ~= nil and (max_seq == nil or n > max_seq) then
                    max_seq = n
                end
            end
            local h1, m1, s1, ms1, h2, m2, s2, ms2 = line:match("(%d+):(%d+):(%d+)[,.](%d+)%s*%-%->%s*(%d+):(%d+):(%d+)[,.](%d+)")
            if h2 ~= nil then
                local sec = tonumber(h2) * 3600 + tonumber(m2) * 60 + tonumber(s2) + tonumber(ms2) / 1000
                if max_end_sec == nil or sec > max_end_sec then
                    max_end_sec = sec
                end
            end
        end
        f:close()
        return max_seq, max_end_sec
    end

    -- Merge chunk files into the final SRT with deduplication and sorting
    local function merge_srt(src_paths)
        local blocks_by_seq = {}
        local seq_list = {}
        for _, cf in ipairs(src_paths) do
            local src = io.open(cf, "r")
            if src ~= nil then
                local content = src:read("*a")
                src:close()
                content = content:gsub("\r\n", "\n"):gsub("\r", "\n") .. "\n\n"
                for block in content:gmatch("(.-)\n\n+") do
                    local trimmed = block:gsub("^%s+", ""):gsub("%s+$", "")
                    if trimmed ~= "" then
                        local seq_str = trimmed:match("^(%d+)\n")
                        if seq_str ~= nil then
                            local seq_num = tonumber(seq_str)
                            if seq_num ~= nil then
                                if blocks_by_seq[seq_num] == nil then
                                    table.insert(seq_list, seq_num)
                                end
                                blocks_by_seq[seq_num] = trimmed
                            end
                        end
                    end
                end
            end
        end

        table.sort(seq_list)
        local final = io.open(session.srt_path, "w")
        if final == nil then return end
        for _, seq in ipairs(seq_list) do
            final:write(blocks_by_seq[seq])
            final:write("\n\n")
        end
        final:close()
    end

    local CHUNK_INIT_MAX_RETRIES = 3
    local CHUNK_INIT_RETRY_DELAY_SECS = 1.0

    local function start_chunk(start_sec, end_sec, retry_count)
        retry_count = retry_count or 0
        if session == nil then return end
        if chunk_py_handle ~= nil then
            msg.warn("start_chunk called but Python process already running")
            return
        end

        session.chunk_index = session.chunk_index + 1
        local ci = session.chunk_index
        local chunk_srt = utils.join_path(chunk_dir, string.format("chunk_%04d.srt", ci))
        local chunk_ipc = utils.join_path(chunk_dir, string.format("chunk_%04d.progress", ci))

        if options.chunk_by_batch then
            msg.info(string.format(
                "Start chunk #%d: from %s (batch size %d lines)",
                ci, format_time(start_sec), options.batch_size
            ))
            show(string.format("translating from %s (%d lines)", format_time(start_sec), options.batch_size))
        else
            msg.info(string.format(
                "Start chunk #%d: [%s - %s]",
                ci, format_time(start_sec), format_time(end_sec)
            ))
            show(string.format("translating %s - %s", format_time(start_sec), format_time(end_sec)))
        end

        local py_extra = {
            "--start-offset", string.format("%.3f", start_sec),
            "--start-seq", session.last_translated_seq .. "",
        }
        if options.chunk_by_batch then
            table.insert(py_extra, "--max-lines")
            table.insert(py_extra, tostring(options.batch_size))
            table.insert(py_extra, "--max-duration")
            table.insert(py_extra, "0")
        else
            table.insert(py_extra, "--max-duration")
            table.insert(py_extra, string.format("%.3f", end_sec - start_sec))
        end

        local chunk_args = build_py_args(py_args, py_script, {
            video_url=video_url,
            ext_sub_url=ext_sub_url,
            sub_track=sub_track,
            output_path=chunk_srt,
            ipc_path=chunk_ipc,
        }, py_extra)
        msg.debug("Execute chunk", utils.format_json(redact_args(chunk_args)))

        -- Clean up previous IPC timer
        if ipc_read_timer ~= nil then
            ipc_read_timer:kill()
            ipc_read_timer = nil
        end

        chunk_py_handle = mp.command_native_async({
            name="subprocess",
            args=chunk_args,
            playback_only=false,
        }, function(success, result, error)
            msg.debug("Chunk #" .. ci .. " exit:", utils.format_json(result))
            local was_killed = result and result.killed_by_us

            -- Clean up IPC timer
            if ipc_read_timer ~= nil then
                ipc_read_timer:kill()
                ipc_read_timer = nil
            end

            chunk_py_handle = nil

            if was_killed then
                -- Session is being cancelled, abort_session already called
                return
            end

            if not success then
                return abort_session("failed to execute command: " .. error)
            end

            if result.status ~= 0 then
                -- mpv on Windows can fail to create the process ("init") with
                -- a non-empty `env`; retry a few times to ride it out.
                if result.error_string == "init" and retry_count < CHUNK_INIT_MAX_RETRIES then
                    msg.warn(string.format(
                        "chunk #%d init failed, retry in %ds (%d/%d)",
                        ci, CHUNK_INIT_RETRY_DELAY_SECS, retry_count + 1, CHUNK_INIT_MAX_RETRIES
                    ))
                    session.chunk_index = session.chunk_index - 1
                    mp.add_timeout(CHUNK_INIT_RETRY_DELAY_SECS, function()
                        start_chunk(start_sec, end_sec, retry_count + 1)
                    end)
                    return
                end
                -- Try to read panic message from IPC
                local panic_msg = read_panic_msg(chunk_ipc)
                local log_path = utils.join_path(output_dir, "llm_subtrans_error.log")
                msg.error("LLM SubTrans chunk #" .. ci .. " failed")
                msg.info("Detailed log written to:", log_path)
                if panic_msg ~= nil then
                    return abort_session(panic_msg .. " (see llm_subtrans_error.log)")
                else
                    return abort_session("script exit with " .. result.status .. " (see llm_subtrans_error.log)")
                end
            end

            -- Read progress to get actual end position
            local ipc = io.open(chunk_ipc, "r")
            local progress = nil
            if ipc ~= nil then
                progress = utils.parse_json(ipc:read("*a"))
                ipc:close()
            end

            -- Record chunk file for concatenation
            -- Only include if the file has content
            local chunk_info = utils.file_info(chunk_srt)
            local lines_in_chunk = 0
            if chunk_info and chunk_info.size > 0 then
                table.insert(session.chunk_files, chunk_srt)

                -- Update translated_end_sec and last_translated_seq from progress and SRT file
                if progress ~= nil and progress["last_timestamp_millis"] ~= nil then
                    local end_ms = progress["last_timestamp_millis"][2]
                    if end_ms > session.translated_end_sec * 1000 then
                        session.translated_end_sec = end_ms / 1000
                    end
                end
                if progress ~= nil and progress["last_seq"] ~= nil then
                    session.last_translated_seq = progress["last_seq"]
                end
                if progress ~= nil and progress["lines_done"] ~= nil then
                    lines_in_chunk = progress["lines_done"]
                end
                -- Also inspect the written chunk SRT directly to ensure accuracy
                local srt_seq, srt_end = get_chunk_srt_stats(chunk_srt)
                if srt_seq ~= nil and srt_seq > session.last_translated_seq then
                    session.last_translated_seq = srt_seq
                end
                if srt_end ~= nil and srt_end > session.translated_end_sec then
                    session.translated_end_sec = srt_end
                end
            else
                msg.info("Chunk #" .. ci .. " produced no subtitles (empty window)")
                -- Advance past the empty window to avoid infinite retries
                if options.chunk_by_batch then
                    local total_sec = mp.get_property_native("duration/full", nil)
                    session.translated_end_sec = total_sec or (session.translated_end_sec + 300)
                else
                    session.translated_end_sec = end_sec
                end
            end

            -- Concatenate all chunks into final SRT
            merge_srt(session.chunk_files)

            -- Reload or add subtitles
            if session.sub_added then
                -- Subsequent chunks: reload
                msg.info("Reload translated subtitles (chunk #" .. ci .. ")")
                mp.command_native({name="sub-reload"})
            else
                -- First chunk: add the translated subtitle track
                msg.info("Add translated subtitles")
                mp.command_native({
                    name="sub-add",
                    url=session.srt_path,
                    title="Translated [" .. (session.lang_code or "trans") .. "]",
                })
                session.sub_added = true
            end

            -- Check for end-of-video or end-of-subtitles
            local total_sec = mp.get_property_native("duration/full", nil)
            local is_eof = progress and (progress["is_eof"] == true or progress["eof"] == true)
            if options.chunk_by_batch and lines_in_chunk > 0 and lines_in_chunk < options.batch_size then
                is_eof = true
            end
            if chunk_info == nil or chunk_info.size == 0 then
                is_eof = true
            end

            if is_eof or (total_sec ~= nil and session.translated_end_sec >= total_sec) then
                show(ASS_COLOR_GREEN .. "all done")
                msg.info("Progressive translation complete")
                remove_ov(5)
                session = nil
                chunk_ov = nil
                if chunk_timer ~= nil then
                    chunk_timer:kill()
                    chunk_timer = nil
                end
                return
            end

            if options.continuous_mode then
                show(string.format("Ready up to %s (translating next batch...)", format_time(session.translated_end_sec)))
                remove_ov(3)
                -- Immediately start next chunk
                local next_start = session.translated_end_sec
                local next_end = next_start + options.pre_translate_seconds
                if total_sec ~= nil and next_end > total_sec then
                    next_end = total_sec
                end
                if options.chunk_by_batch or next_end > next_start then
                    start_chunk(next_start, next_end)
                end
            else
                local advance_at = math.max(0, session.translated_end_sec - options.advance_threshold_seconds)
                show(string.format("Ready up to %s (auto-advances at %s)", format_time(session.translated_end_sec), format_time(advance_at)))
                remove_ov(4)
            end
        end)

        -- Set up IPC progress reader for this chunk
        local last_progress_seq = -1
        ipc_read_timer = mp.add_periodic_timer(1, function()
            local ipc = io.open(chunk_ipc, "r")
            if ipc == nil then return end
            local prog = utils.parse_json(ipc:read("*a"))
            ipc:close()
            if prog == nil then return end

            -- Check if Python reported rate limiting or network retry
            if prog["status"] == "rate_limited" then
                local retry_sec = prog["retry_in"] or 3
                local attempt = prog["attempt"] or 1
                local max_retries = prog["max_retries"] or 3
                show(string.format("{\\c&H00FFFF&}Rate limit (429) - retry in %ds (%d/%d)", retry_sec, attempt, max_retries))
                return
            elseif prog["status"] == "network_retry" then
                local retry_sec = prog["retry_in"] or 2
                local attempt = prog["attempt"] or 1
                local max_retries = prog["max_retries"] or 3
                show(string.format("{\\c&H00FFFF&}Connection/API drop - retry in %ds (%d/%d)", retry_sec, attempt, max_retries))
                return
            end

            if prog["last_seq"] == nil then return end
            if prog["last_seq"] <= last_progress_seq then return end
            last_progress_seq = prog["last_seq"]

            -- Update OSD progress
            if prog["last_timestamp_millis"] ~= nil then
                local end_ms = prog["last_timestamp_millis"][2]
                local model_name = options.model ~= "" and options.model or "default"
                local short_model = model_name:match("[^/]+$") or model_name

                if options.chunk_by_batch then
                    local target_lines = options.batch_size
                    local lines_done = prog["lines_done"] or 0
                    local chunk_pct = target_lines > 0 and math.min(100, math.max(0, math.floor(lines_done / target_lines * 100))) or 0
                    local bar = make_progress_bar(chunk_pct, 12)
                    show(string.format("[%s] #%d %s %d%% (%d/%d lines, %s)",
                        short_model,
                        ci,
                        bar,
                        chunk_pct,
                        lines_done,
                        target_lines,
                        format_time(end_ms / 1000)))
                else
                    local lines_info = prog["lines_done"] and string.format(" (%d lines)", prog["lines_done"]) or ""
                    local chunk_dur = end_sec - start_sec
                    local chunk_done = (end_ms / 1000) - start_sec
                    local chunk_pct = (chunk_dur and chunk_dur > 0) and math.min(100, math.max(0, math.floor(chunk_done / chunk_dur * 100))) or 0
                    local bar = make_progress_bar(chunk_pct, 12)
                    show(string.format("[%s] #%d %s %d%% (%s / %s)%s",
                        short_model,
                        ci,
                        bar,
                        chunk_pct,
                        format_time(end_ms / 1000),
                        format_time(end_sec),
                        lines_info))
                end

                -- Incrementally load translated content:
                -- first time: as soon as any content is translated
                -- afterwards: when playback approaches the end of what was loaded
                local pos_ms = mp.get_property_native("time-pos", 0) * 1000
                local should_load = false
                if not session.sub_added then
                    should_load = end_ms > pos_ms
                elseif session.last_loaded_end_ms ~= nil then
                    should_load = end_ms > pos_ms and session.last_loaded_end_ms - pos_ms < 10 * 1000
                end

                if should_load then
                    local now = mp.get_time()
                    if session.last_reload_time == nil or now - session.last_reload_time >= 2 then
                        session.last_reload_time = now
                        session.last_loaded_end_ms = end_ms

                        -- Merge completed chunks + current chunk partial content
                        local all_paths = {}
                        for _, f in ipairs(session.chunk_files) do
                            table.insert(all_paths, f)
                        end
                        table.insert(all_paths, chunk_srt)
                        merge_srt(all_paths)

                        if session.sub_added then
                            msg.info("Reload translated subtitles (incremental)")
                            mp.command_native({name="sub-reload"})
                        else
                            msg.info("Add translated subtitles")
                            mp.command_native({
                                name="sub-add",
                                url=session.srt_path,
                                title="Translated [" .. (session.lang_code or "trans") .. "]",
                            })
                            session.sub_added = true
                        end
                    end
                end
            end
        end)
    end

    -- Periodic monitor: check if we need to start the next chunk
    chunk_timer = mp.add_periodic_timer(3, function()
        if session == nil then
            -- Session was cleaned up
            if chunk_timer ~= nil then
                chunk_timer:kill()
                chunk_timer = nil
            end
            return
        end

        local pos = mp.get_property_native("time-pos", 0)
        if pos == nil then return end

        local translated_end = session.translated_end_sec
        local threshold = options.advance_threshold_seconds

        -- Check if playback is past the translated content (user seeked forward)
        -- or approaching the end of translated content
        local need_more = false
        if options.continuous_mode then
            -- In continuous mode, translate sequentially chunk by chunk without skipping gaps
            need_more = true
        elseif pos > translated_end then
            -- User seeked past translated content (in on-demand mode, jump to current playback position)
            need_more = true
            session.translated_end_sec = pos  -- jump to current position
        elseif translated_end - pos <= threshold then
            -- Approaching end of translated content
            need_more = true
        end

        -- Check end of video
        local total_sec = mp.get_property_native("duration/full", nil)
        if total_sec ~= nil and translated_end >= total_sec then
            need_more = false
            if chunk_py_handle == nil then
                show(ASS_COLOR_GREEN .. "all done")
                msg.info("Progressive translation complete")
                remove_ov(3)
                session = nil
                chunk_ov = nil
                chunk_timer:kill()
                chunk_timer = nil
            end
            return
        end

        if need_more and chunk_py_handle == nil then
            local next_start = session.translated_end_sec
            local next_end = next_start + options.pre_translate_seconds
            if total_sec ~= nil and next_end > total_sec then
                next_end = total_sec
            end
            if options.chunk_by_batch or next_end > next_start then
                start_chunk(next_start, next_end)
            end
        end
    end)

    -- Start the first chunk
    local first_end = start_pos_sec + options.pre_translate_seconds
    local total_dur = mp.get_property_native("duration/full", nil)
    if total_dur ~= nil and first_end > total_dur then
        first_end = total_dur
    end
    start_chunk(start_pos_sec, first_end)
end

function progressive_translate()
    local ok, err = pcall(do_progressive_translate)
    if not ok then
        msg.error("progressive_translate fatal error: " .. tostring(err))
        session = nil
        chunk_py_handle = nil
        local _, show, remove_ov = create_osd()
        show(ASS_COLOR_RED .. "Error: " .. tostring(err))
        remove_ov(6)
    end
end

require "mp.options".read_options(options, "llm_subtrans")
mp.add_key_binding('alt+t', "subtrans", progressive_translate)
mp.add_key_binding('alt+shift+t', "subtrans-full", llm_subtrans_translate)
mp.add_key_binding('Alt+T', "subtrans-full-alt", llm_subtrans_translate)
mp.add_key_binding('Alt+Shift+T', "subtrans-full-upper", llm_subtrans_translate)

-- Clean up temporary chunk directories when mpv exits
mp.register_event("shutdown", function()
    for _, chunk_dir in ipairs(created_chunk_dirs) do
        cleanup_chunk_dir(chunk_dir)
    end
    created_chunk_dirs = {}
    for _, temp_file in ipairs(created_temp_files) do
        os.remove(temp_file)
    end
    created_temp_files = {}
end)
