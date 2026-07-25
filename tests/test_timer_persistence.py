from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ROOT / "StackChan/firmware/main/apps/common/timer"


def test_production_timer_persistence_round_trip(tmp_path: Path):
    source = tmp_path / "timer_persistence_test.cpp"
    binary = tmp_path / "timer_persistence_test"
    source.write_text(
        r'''
#include "timer_persistence.h"
#include <cassert>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

struct FakeSettings {
    std::map<std::string, int32_t> ints;
    std::map<std::string, std::string> strings;
    std::map<std::string, bool> bools;

    int32_t GetInt(const std::string& key, int32_t fallback = 0) {
        auto it = ints.find(key); return it == ints.end() ? fallback : it->second;
    }
    std::string GetString(const std::string& key, const std::string& fallback = "") {
        auto it = strings.find(key); return it == strings.end() ? fallback : it->second;
    }
    bool GetBool(const std::string& key, bool fallback = false) {
        auto it = bools.find(key); return it == bools.end() ? fallback : it->second;
    }
    void SetInt(const std::string& key, int32_t value) { ints[key] = value; }
    void SetString(const std::string& key, const std::string& value) { strings[key] = value; }
    void SetBool(const std::string& key, bool value) { bools[key] = value; }
    void EraseAll() { ints.clear(); strings.clear(); bools.clear(); }
};

int main() {
    using tools::timer_persistence::Record;
    FakeSettings settings;
    std::vector<Record> records;
    for (int i = 0; i < 8; ++i) {
        records.push_back({i + 1, "timer-" + std::to_string(i), "done", 60u + i,
                           30u + i, 2200000000LL + i, i % 2 == 0});
    }
    tools::timer_persistence::save(settings, records, 42);
    int next_id = 0;
    auto restored = tools::timer_persistence::load(settings, next_id);
    assert(next_id == 42);
    assert(restored == records);
    assert(restored.front().paused);
    assert(!restored.back().paused);

    records.push_back({9, "overflow", "ignored", 10, 10, 1780000100, false});
    tools::timer_persistence::save(settings, records, 43);
    restored = tools::timer_persistence::load(settings, next_id);
    assert(restored.size() == 8);
    return 0;
}
''',
        encoding="utf-8",
    )
    subprocess.run(
        ["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I", str(INCLUDE), str(source), "-o", str(binary)],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
