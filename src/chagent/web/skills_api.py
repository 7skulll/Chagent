"""User skill management: ordinary authenticated users own their tenant store."""
import base64
import binascii
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from importlib.resources import files
from pydantic import BaseModel, ConfigDict, Field

from chagent.skills import SKILL_NAMES, read_skill
from chagent.user_skills import UserSkillStore, parse_import
from chagent.web.deps import Current

router = APIRouter(prefix='/api', tags=['用户技能'])
page_router = APIRouter()


@page_router.get('/skills', response_class=HTMLResponse)
async def skills_page():
    return HTMLResponse(files('chagent.web').joinpath('static', 'skills.html').read_text(),
                        headers={'Cache-Control': 'no-store'})


class ImportSkill(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filename: str = Field(max_length=255)
    data: str = Field(max_length=2_800_000)


class SkillAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)


def public(row):
    return {key: row[key] for key in ('id', 'name', 'description', 'content', 'sha256', 'enabled', 'revision')} | {
        'files': sorted(row['files']), 'source': 'user'}


@router.get('/skills')
async def list_skills(current: Current):
    rows = await UserSkillStore(current.runner.runs.database).list()
    return {'system': [{'name': name, 'content': read_skill(name), 'source': 'builtin'} for name in SKILL_NAMES],
            'user': [public(row) for row in rows]}


@router.post('/user-skills/import', status_code=201)
async def import_skill(value: ImportSkill, current: Current):
    try:
        data = base64.b64decode(value.data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('上传内容编码无效') from None
    skill = parse_import(value.filename, data)
    return public(await UserSkillStore(current.runner.runs.database).import_skill(skill))


async def change(identity, value, current, *, enabled=None, delete=False):
    store = UserSkillStore(current.runner.runs.database)
    await store.change(identity, value.expected_revision, enabled=enabled, delete=delete)
    cancelled = []
    if delete or not enabled:
        for run_id, state in list(current.runner._states.items()):
            if str(identity) in state.loaded_user_skills:
                if current.runner.cancel(run_id):
                    cancelled.append(str(run_id))
    return {'ok': True, 'cancellation_requested': cancelled}


@router.post('/user-skills/{identity}/activate')
async def activate(identity: UUID, value: SkillAction, current: Current):
    return await change(identity, value, current, enabled=True)


@router.post('/user-skills/{identity}/disable')
async def disable(identity: UUID, value: SkillAction, current: Current):
    return await change(identity, value, current, enabled=False)


@router.delete('/user-skills/{identity}')
async def delete(identity: UUID, value: SkillAction, current: Current):
    return await change(identity, value, current, delete=True)
