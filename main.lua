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
}

local ASS_COLOR_RED = "{\\c&H8899FF&}"
local ASS_COLOR_GREEN = "{\\c&H99FF88&}"
local IS_WINDODWS = mp.get_property("vo-mmcss-profile") ~= nil  -- Windows only property

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
        ov.data = "{\\b1}{\\fs32}LLM SubTrans{\\b0} - " .. msg
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

--- Resolve output directory and srt file path
-- @return output_dir, srt_path
local function resolve_output_path()
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
    local srt_path = utils.join_path(output_dir, mp.get_property("filename/no-ext") .. ".srt")
    return output_dir, srt_path
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
        "--api-key", options.api_key,
        "--model", options.model,
        "--base-url", options.base_url,
        "--ffmpeg-bin", options.ffmpeg_bin,
        "--video-url", opts.video_url,
        "--subtitle-url", opts.ext_sub_url,
        "--sub-track-id", opts.sub_track.id - 1 .. "",
        "--batch-size", options.batch_size .. "",
        "--dest-lang", options.dest_lang,
        "--extra-prompt", options.extra_prompt,
        "--output-path", opts.output_path,
        "--ipc-path", opts.ipc_path,
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

function llm_subtrans_translate()
    -- check running
    if running then
        if py_handle ~= nil then
            msg.info("kill python script (user reuqest)")
            mp.abort_async_command(py_handle)
        else
            msg.info("already running")
        end
        return
    end
    msg.info("Start subtitle tranlsate")
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
    local output_dir, srt_path = resolve_output_path()
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
            if panic ~= nil then
                return abort(panic)
            else
                return abort("script exit with " .. result.status .. " " .. result.error_string)
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
            msg.info("Set tranlsated substitles")
            mp.command_native({
                name="sub-add",
                url=srt_path,
                title="Translated",
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
        local total_sec = mp.get_property_native("duration/full", nil)
        local pos_sec = progress["last_timestamp_millis"][2] / 1000
        if total_sec == nil then
            show("translating")
        elseif pos_sec >= total_sec then
            show("finishing")
        else
            show(string.format("%d%%", pos_sec / total_sec * 100))
        end
    end)

end

-- Session state for progressive translation
local session = nil  -- {chunk_index, translated_end_sec, chunk_files, chunk_dir, output_dir, srt_path, sub_track}
local chunk_py_handle = nil
local chunk_timer = nil
local chunk_ov = nil
local created_chunk_dirs = {}  -- chunk dirs created this mpv session, cleaned on shutdown
local created_temp_files = {}  -- temp files created this mpv session, cleaned on shutdown

local function format_time(sec)
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

function progressive_translate()
    -- If a session is active, cancel it
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

    msg.info("Start progressive subtitle translation")
    session = {}

    -- OSD overlay
    local ov, show, remove_ov = create_osd()
    chunk_ov = ov

    -- Abort helper
    local function abort_session(error_msg)
        if error_msg ~= nil then
            msg.warn("Progressive translate abort:", error_msg)
            show(ASS_COLOR_RED .. error_msg)
            remove_ov(5)
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
    local output_dir, srt_path = resolve_output_path()
    msg.info("Save file to", srt_path)
    session.output_dir = output_dir
    session.srt_path = srt_path

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

    -- Merge chunk files into the final SRT
    local function merge_srt(src_paths)
        local final = io.open(session.srt_path, "w")
        if final == nil then return end
        for _, cf in ipairs(src_paths) do
            local src = io.open(cf, "r")
            if src ~= nil then
                final:write(src:read("*a"))
                src:close()
            end
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

        msg.info(string.format(
            "Start chunk #%d: [%s - %s]",
            ci, format_time(start_sec), format_time(end_sec)
        ))
        show(string.format("translating %s - %s", format_time(start_sec), format_time(end_sec)))

        local chunk_args = build_py_args(py_args, py_script, {
            video_url=video_url,
            ext_sub_url=ext_sub_url,
            sub_track=sub_track,
            output_path=chunk_srt,
            ipc_path=chunk_ipc,
        }, {
            "--start-offset", string.format("%.3f", start_sec),
            "--max-duration", string.format("%.3f", end_sec - start_sec),
            "--start-seq", session.last_translated_seq .. "",
        })
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
                if panic_msg ~= nil then
                    return abort_session(panic_msg)
                else
                    return abort_session("script exit with " .. result.status .. " " .. (result.error_string or "unknown error"))
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
            if chunk_info and chunk_info.size > 0 then
                table.insert(session.chunk_files, chunk_srt)

                -- Update translated_end_sec from progress
                if progress ~= nil and progress["last_timestamp_millis"] ~= nil then
                    local end_ms = progress["last_timestamp_millis"][2]
                    if end_ms > session.translated_end_sec * 1000 then
                        session.translated_end_sec = end_ms / 1000
                    end
                end
                -- Update last_translated_seq for precise next-chunk boundary
                if progress ~= nil and progress["last_seq"] ~= nil then
                    session.last_translated_seq = progress["last_seq"]
                end
            else
                msg.info("Chunk #" .. ci .. " produced no subtitles (empty window)")
                -- Advance past the empty window to avoid infinite retries
                session.translated_end_sec = end_sec
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
                    title="Translated",
                })
                session.sub_added = true
            end

            -- Check for end-of-video
            local total_sec = mp.get_property_native("duration/full", nil)
            if total_sec ~= nil and session.translated_end_sec >= total_sec then
                show(ASS_COLOR_GREEN .. "all done")
                msg.info("Progressive translation complete")
                remove_ov(5)
                session = nil
                chunk_ov = nil
                return
            end

            show("translated to " .. format_time(session.translated_end_sec) .. " (waiting)")
        end)

        -- Set up IPC progress reader for this chunk
        local last_progress_seq = -1
        ipc_read_timer = mp.add_periodic_timer(1, function()
            local ipc = io.open(chunk_ipc, "r")
            if ipc == nil then return end
            local prog = utils.parse_json(ipc:read("*a"))
            ipc:close()
            if prog == nil then return end
            if prog["last_seq"] == nil then return end
            if prog["last_seq"] <= last_progress_seq then return end
            last_progress_seq = prog["last_seq"]

            -- Update OSD progress
            if prog["last_timestamp_millis"] ~= nil then
                local end_ms = prog["last_timestamp_millis"][2]
                show(string.format("translating %s / %s",
                    format_time(end_ms / 1000),
                    format_time(end_sec)))

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
                                title="Translated",
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
        if pos > translated_end then
            -- User seeked past translated content
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
            if next_end > next_start then
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

require "mp.options".read_options(options, "llm_subtrans")
mp.add_key_binding('alt+t', "subtrans", progressive_translate)
mp.add_key_binding('alt+shift+t', "subtrans-full", llm_subtrans_translate)

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
