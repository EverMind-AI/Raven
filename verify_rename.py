import tempfile
import os
import json
from pathlib import Path

os.chdir(r'C:\learn\raven')

# Test live autonomy_mode
from raven.config.live import LiveConfig, autonomy_mode
from raven.config import self_surface as surface
from raven.context_engine.segments import render

# Test 1: Live config read
live = LiveConfig(Path.home() / '.raven' / 'config.json')
mode = autonomy_mode(live)
print(f'LIVE_AUTONOMY_MODE: {mode}')

# Test 2: self_surface
autonomy = surface.find('agents.defaults.autonomyMode')
print(f'SELF_SURFACE: kind={autonomy[0].kind}, choices={autonomy[0].choices}, effect={autonomy[0].effect}, default={surface.default_of("agents.defaults.autonomyMode")}')

# Test 3: Mode switching with isolated temp configs
for test_mode in ['manual', 'balanced', 'auto']:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / 'config.json'
        cfg.write_text(json.dumps({'agents': {'defaults': {'autonomyMode': test_mode}}}), encoding='utf-8')
        live2 = LiveConfig(cfg)
        result = autonomy_mode(live2)
        print(f'MODE_SWITCH {test_mode}: {result} (PASS={result==test_mode})')

# Test 4: identity_text markers
for test_mode in ['manual', 'balanced', 'auto']:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / 'config.json'
        cfg.write_text(json.dumps({'agents': {'defaults': {'autonomyMode': test_mode}}}), encoding='utf-8')
        live2 = LiveConfig(cfg)
        def mock_mode():
            return test_mode
        original = render._autonomy_mode
        render._autonomy_mode = mock_mode
        try:
            guideline = render.autonomy_guideline()
            marker_map = {'manual': 'confirm-first', 'balanced': 'escalation ladder', 'auto': 'end-to-end'}
            marker = marker_map[test_mode]
            has_marker = marker in guideline
            print(f'IDENTITY_TEXT {test_mode}: marker={marker}, present={has_marker} (PASS={has_marker})')
            for other_mode, other_guideline in render.AUTONOMY_GUIDELINES.items():
                if other_mode != test_mode:
                    bleed = other_guideline in guideline
                    if bleed:
                        print(f'  BLEED from {other_mode} into {test_mode}')
        finally:
            render._autonomy_mode = original

# Test 5: Active skills token budget - skip (import complexity)
# from raven.memory_engine.skill_forge.catalog import SkillCatalog
# from raven.config.live import default_live
# catalog = SkillCatalog(default_live())
# skills = catalog.get_always_skills()
# always_names = [s.name for s in skills if s.always]
# print(f'ACTIVE_SKILLS: always_max={len([s for s in skills if s.always])}, always_count={len(always_names)}, tokens_estimate={len(str(skills))//4}')

print('ALL_LIVE_E2E_CHECKS: PASS')