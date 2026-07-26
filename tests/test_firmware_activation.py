from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "StackChan/firmware/xiaozhi-esp32/main/application.cc"
XIAOZHI_PATCH = ROOT / "StackChan/firmware/patches/xiaozhi-esp32.patch"
OTA = ROOT / "StackChan/firmware/xiaozhi-esp32/main/ota.cc"
WIFI_BOARD = ROOT / "StackChan/firmware/xiaozhi-esp32/main/boards/common/wifi_board.cc"
STACKCHAN_DISPLAY = ROOT / "StackChan/firmware/main/hal/board/stackchan_display.cc"
HAL = ROOT / "StackChan/firmware/main/hal/hal.cpp"
HAL_BRIDGE = ROOT / "StackChan/firmware/main/hal/board/hal_bridge.cc"
SFX_DIR = ROOT / "StackChan/firmware/main/assets/sfx"
SDKCONFIG_DEFAULTS_LOCAL = ROOT / "StackChan/firmware/sdkconfig.defaults.local"


def test_agent_activation_uses_cached_protocol_without_background_ota() -> None:
    source = APPLICATION.read_text(encoding="utf-8")
    activation = source[source.index("void Application::ActivationTask()") : source.index("void Application::CheckAssetsVersion()")]

    assert activation.index("LoadCachedWebsocketConfig") < activation.index("InitializeProtocol")
    assert "Background version refresh" not in activation
    assert '"Agent activation ready in %d ms' in activation
    assert "%lld" not in activation


def test_ota_url_remains_runtime_configurable() -> None:
    source = OTA.read_text(encoding="utf-8")
    getter = source[source.index("std::string Ota::GetCheckVersionUrl()") : source.index("std::unique_ptr<Http> Ota::SetupHttp()")]

    assert 'Settings settings("wifi", false)' in getter
    assert 'settings.GetString("ota_url")' in getter
    assert "CONFIG_OTA_URL" in getter


def test_agent_wifi_scan_retry_does_not_pause_for_ten_seconds() -> None:
    source = WIFI_BOARD.read_text(encoding="utf-8")

    assert "config.station_scan_min_interval_seconds = 1" in source
    assert "config.station_scan_max_interval_seconds = 30" in source


def test_agent_reuses_an_existing_wifi_connection() -> None:
    source = WIFI_BOARD.read_text(encoding="utf-8")
    callback = source.index("wifi_manager.SetEventCallback")
    reuse = source.index("if (wifi_manager.IsConnected())", callback)
    reconnect = source.index("TryWifiConnect();", reuse)

    assert callback < reuse < reconnect
    assert "OnNetworkEvent(NetworkEvent::Connected, wifi_manager.GetSsid())" in source[reuse:reconnect]


def test_stackchan_notifications_are_visible_in_the_avatar_bubble() -> None:
    source = STACKCHAN_DISPLAY.read_text(encoding="utf-8")
    notification = source[
        source.index("void StackChanAvatarDisplay::ShowNotification") :
    ]

    assert "stackchan.avatar().setSpeech(notification)" in notification
    assert "ShowNotification: %s" in notification


def test_successful_websocket_open_clears_stale_error_audio() -> None:
    source = APPLICATION.read_text(encoding="utf-8")
    opened = source[
        source.index("protocol_->OnAudioChannelOpened") :
        source.index("protocol_->OnAudioChannelClosed")
    ]

    assert "audio_service_.ResetDecoder();" in opened
    assert "last_error_message_.clear();" in opened


def test_agent_preconnects_bridge_and_shows_ready_prompt() -> None:
    source = APPLICATION.read_text(encoding="utf-8")
    activation_done = source[
        source.index("void Application::HandleActivationDoneEvent()") :
        source.index("void Application::ActivationTask()")
    ]
    preconnect = source[
        source.index("void Application::StartBridgePreconnect()") :
        source.index("void Application::CheckAssetsVersion()")
    ]
    wake = source[
        source.index("void Application::HandleWakeWordDetectedEvent()") :
        source.index("void Application::ContinueWakeWordInvoke")
    ]

    assert '"WiFi 已连接，正在连接 Bridge..."' in activation_done
    assert "StartBridgePreconnect();" in activation_done
    assert "protocol_->OpenAudioChannel()" in preconnect
    assert '"Nanobot 已就绪，请说唤醒词"' in preconnect
    assert '"Bridge 连接失败，请检查电脑端服务"' in preconnect
    assert '"Bridge 正在连接，请稍等..."' in wake


def test_agent_preconnect_changes_are_recorded_in_xiaozhi_patch() -> None:
    patch = XIAOZHI_PATCH.read_text(encoding="utf-8")

    assert '"WiFi 已连接，正在连接 Bridge..."' in patch
    assert "StartBridgePreconnect();" in patch
    assert '"Nanobot 已就绪，请说唤醒词"' in patch
    assert '"Bridge 连接失败，请检查电脑端服务"' in patch
    assert '"Bridge 正在连接，请稍等..."' in patch


def test_speaking_state_disables_wake_word_to_avoid_self_interruption() -> None:
    source = APPLICATION.read_text(encoding="utf-8")
    speaking = source[
        source.index("case kDeviceStateSpeaking:") :
        source.index("case kDeviceStateWifiConfiguring:")
    ]

    assert "audio_service_.EnableWakeWordDetection(false);" in speaking
    assert "EnableWakeWordDetection(audio_service_.IsAfeWakeWord())" not in speaking


def test_timer_notification_is_rendered_by_the_stackchan_owner_task() -> None:
    source = HAL.read_text(encoding="utf-8")
    scheduler = source[
        source.index("static void _schedule_notification") :
        source.index("void Hal::startXiaozhi()")
    ]
    update_task = source[
        source.index("static void _stackchan_update_task") :
        source.index("static void _schedule_notification")
    ]

    assert "pending_notifications.push_back" in scheduler
    assert "hal_bridge::app_schedule" not in scheduler
    assert "pop_notification(notification)" in update_task
    assert "avatar().addDecorator" in update_task
    assert "xiaozhi_ready && is_setup_done && xiaozhi_idle" in update_task
    assert update_task.index("vTaskDelay(pdMS_TO_TICKS(100))") < update_task.index("LvglLockGuard lock")


def test_notification_audio_is_serialized_on_the_application_event_loop() -> None:
    source = HAL_BRIDGE.read_text(encoding="utf-8")
    play_sound = source[
        source.index("void app_play_sound") :
        source.index("void app_schedule")
    ]

    assert "Application::GetInstance().Schedule" in play_sound
    assert "Application::GetInstance().PlaySound(sound)" in play_sound


def test_embedded_sound_effects_match_audio_service_opus_format() -> None:
    converter = (SFX_DIR / "convert_cmd.sh").read_text(encoding="utf-8")

    assert "-ac 1" in converter
    assert "-ar 48000" in converter
    assert "-frame_duration 60" in converter

    for name in ("new_notification.ogg", "camera_shutter.ogg"):
        data = (SFX_DIR / name).read_bytes()
        opus_head = data.index(b"OpusHead")
        assert data[opus_head + 9] == 1
        assert int.from_bytes(data[opus_head + 12 : opus_head + 16], "little") == 48000


def test_flash_coredump_is_enabled_for_physical_panic_diagnostics() -> None:
    defaults = SDKCONFIG_DEFAULTS_LOCAL.read_text(encoding="utf-8")

    assert "CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y" in defaults
    assert "CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF=y" in defaults
    assert "CONFIG_ESP_COREDUMP_MAX_TASKS_NUM=8" in defaults
    assert "CONFIG_FREERTOS_ISR_STACKSIZE=2096" in defaults
