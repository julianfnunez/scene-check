import asyncio
from types import SimpleNamespace
import pytest
from agent import check_timeout
from settings import Configuration
from service import CheckService, PlanError, BusyError
from web import aggregate_run


def config():
    return Configuration.model_validate({
        'sensors': {'camera': {'name': 'Camera'}},
        'scenes': {
            'girls': {'name': 'Girls', 'active': True, 'sensors': {'camera': {}},
                      'conditions': ['Three chickens visible']},
            'chicky': {'name': 'Chicky', 'active': False, 'sensors': {'camera': {}},
                       'conditions': ['One chicken visible']},
        },
    })


def test_parent_scenes_rejected():
    raw = config().model_dump()
    raw['scenes']['parent'] = {'name': 'Parent', 'scenes': ['girls'], 'conditions': ['both fine']}
    with pytest.raises(ValueError):
        Configuration.model_validate(raw)


def test_active_snapshot_and_manual_inactive():
    async def main():
        c = config()
        state = SimpleNamespace(config=c, config_error=None, active=None, task=None)
        runs=[]
        async def worker(record, scene_ids):
            runs.append(scene_ids)
            state.active = None
        service=CheckService(state, lambda _:None, worker, [])
        first=service.submit()
        assert first['scene_ids']==['girls']
        assert first['scene_id'] is None
        await state.task
        second=service.submit('chicky')
        assert second['scene_ids']==['chicky']
        await state.task
        assert runs==[['girls'], ['chicky']]
        c.scenes['chicky'].active=True
        both=service.submit()
        assert both['scene_ids']==['girls','chicky']
        with pytest.raises(BusyError):
            service.submit('girls')
        await state.task
    asyncio.run(main())


def test_no_active_scenes():
    async def main():
        c=config()
        c.scenes['girls'].active=False
        state=SimpleNamespace(config=c, config_error=None, active=None)
        service=CheckService(state, lambda _:None, lambda *a: None, [])
        with pytest.raises(PlanError, match='No active'):
            service.submit()
    asyncio.run(main())


@pytest.mark.parametrize('statuses, expected', [
    (['normal','normal'],'normal'), (['normal','unknown'],'unknown'),
    (['unknown','needs_attention'],'needs_attention'),
    (['normal','needs_attention'],'needs_attention'),
])
def test_run_aggregation(statuses,expected):
    result, summary=aggregate_run([{'status':s} for s in statuses])
    assert result==expected
    assert summary==f"{statuses.count('normal')}/{len(statuses)} scenes normal."


def test_scene_timeout_is_independent():
    c=config()
    assert check_timeout(c.scenes['girls'],c)==240
