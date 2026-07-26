from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "StackChan/firmware/xiaozhi-esp32/main/application.cc"
OTA = ROOT / "StackChan/firmware/xiaozhi-esp32/main/ota.cc"
WIFI_BOARD = ROOT / "StackChan/firmware/xiaozhi-esp32/main/boards/common/wifi_board.cc"
STACKCHAN_DISPLAY = ROOT / "StackChan/firmware/main/hal/board/stackchan_display.cc"
HAL = ROOT / "StackChan/firmware/main/hal/hal.cpp"
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


def test_flash_coredump_is_enabled_for_physical_panic_diagnostics() -> None:
    defaults = SDKCONFIG_DEFAULTS_LOCAL.read_text(encoding="utf-8")

    assert "CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y" in defaults
    assert "CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF=y" in defaults
    assert "CONFIG_ESP_COREDUMP_MAX_TASKS_NUM=8" in defaults
    assert "CONFIG_FREERTOS_ISR_STACKSIZE=2096" in defaults
