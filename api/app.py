import kubernetes_asyncio
import logging
import re

from asgi_tools import App, ResponseError
from datetime import datetime, timezone

from anarchy import Anarchy
from anarchyaction import AnarchyAction
from anarchygovernor import AnarchyGovernor
from anarchyrun import AnarchyRun
from anarchyrunner import AnarchyRunner
from anarchyrunnerpod import AnarchyRunnerPod
from anarchysubject import AnarchySubject

app = App()

@app.on_startup
async def on_startup():
    await Anarchy.on_startup()
    await AnarchyGovernor.on_startup()
    await AnarchyRunner.on_startup()
    await AnarchyRunnerPod.on_startup()
    await AnarchyAction.on_startup()
    await AnarchyRun.on_startup()

@app.on_shutdown
async def on_shutdown():
    await AnarchyRun.on_shutdown()
    await AnarchyRunnerPod.on_shutdown()
    await AnarchyRunner.on_shutdown()
    await AnarchyGovernor.on_shutdown()
    await Anarchy.on_shutdown()

@app.route('/action/{anarchy_action_name}', methods=['POST'])
async def post_action(request):
    """
    Action callback with parameter to indicate the callback name.
    """
    return await handle_action_callback(request)

@app.route('/action/{anarchy_action_name}/{callback_name}', methods=['POST'])
async def post_action_with_callback_name(request):
    """
    Action callback with callback name in path.
    """
    return await handle_action_callback(request)

async def handle_action_callback(request):
    anarchy_action_name = request.path_params['anarchy_action_name']
    callback_name = request.path_params.get('callback_name')

    try:
        anarchy_action = await AnarchyAction.get(anarchy_action_name)
    except kubernetes_asyncio.client.rest.ApiException as e:
        if e.status == 404:
            raise ResponseError.NOT_FOUND(f"AnarchyAction {anarchy_action_name} not found.")
        else:
            raise

    anarchy_action.check_callback_auth(request)

    if anarchy_action.is_finished:
        logging.info("Received callback on finished {anarchy_action}")
        return {"status": "ok"}

    anarchy_subject = await anarchy_action.get_subject()
    anarchy_governor = await anarchy_subject.get_governor()
    action_config = anarchy_governor.get_action_config(anarchy_action.action)
    if not action_config:
        raise ResponseError.NOT_FOUND(f"{anarchy_action} action {anarchy_action.action} not found?")

    callback_data = await request.json()
    await anarchy_action.handle_callback(
        callback_data = callback_data,
        callback_name = callback_name,
    )
    return {"status": "ok"}


@app.route('/run', methods=['GET'])
async def get_run(request):
    anarchy_runner, anarchy_runner_pod = AnarchyRunnerPod.get_from_request(request)

    await AnarchyRun.handle_any_lost_runs(anarchy_runner_pod)

    if anarchy_runner_pod.is_deleting:
        logging.info(f"Not giving AnarchyRun to {anarchy_runner_pod} because it is being deleted")
        return None
    elif anarchy_runner_pod.is_marked_for_termination:
        logging.info(f"Deleting {anarchy_runner_pod} that is marked for termination")
        await anarchy_runner_pod.delete()
        return None

    anarchy_run = await AnarchyRun.get_run_for_runner_pod(anarchy_runner, anarchy_runner_pod)
    if not anarchy_run:
        return None

    handler = anarchy_run.get_handler()
    anarchy_governor = await anarchy_run.get_governor()
    anarchy_subject = await anarchy_run.get_subject()

    if handler['type'] == 'subjectEvent':
        return {
            'handler': handler,
            'governor': await anarchy_governor.export(),
            'subject': await anarchy_subject.export(anarchy_run.subject_vars),
            'run': await anarchy_run.export(),
        }

    anarchy_action = await anarchy_run.get_action()
    await anarchy_action.json_patch_status([{
        "op": "add",
        "path": "/status/state",
        "value": "running",
    }])

    return {
        'handler': handler,
        'governor': await anarchy_governor.export(),
        'subject': await anarchy_subject.export(anarchy_run.subject_vars),
        'action': await anarchy_action.export(),
        'run': await anarchy_run.export(),
    }

    return resp

@app.route('/run/{anarchy_run_name}', methods=['POST'])
async def post_run(request):
    """
    Receive result from AnarchyRun
    """
    anarchy_runner, anarchy_runner_pod = AnarchyRunnerPod.get_from_request(request)
    anarchy_run_name = request.path_params['anarchy_run_name']
    anarchy_run = await AnarchyRun.get(anarchy_run_name)

    if anarchy_run.runner_state != anarchy_runner_pod.name:
        logging.info(
            f"{anarchy_run} has runner state {anarchy_run.runner_state} but run posted by {anarchy_runner_pod}"
        )
        raise ResponseError.BAD_REQUEST("Runner state mismatch")

    request_data = await request.json()
    result = request_data['result']
    result_status = result['status']
    result_status_message = result.get('statusMessage')

    if result_status == 'failed':
        logging.warning(f"{anarchy_runner_pod} posted failed result for {anarchy_run}: {result_status_message}")
        await anarchy_run.set_runner_state_failed(result)
    elif result_status == 'successful':
        logging.info(f"{anarchy_runner_pod} posted successful result for {anarchy_run}")
        await anarchy_run.set_runner_state_successful(result)
    else:
        logging.info(f"{anarchy_runner_pod} sent unknown result status {result_status} for {anarchy_run}")
        raise ResponseError.BAD_REQUEST(f"Unknown result status {result_status}")

    return {"success": True}

@app.route('/run/subject/{anarchy_subject_name}', methods=['PATCH'])
async def patch_subject(request):
    """
    Executing AnarchyRun request to patch its AnarchySubject
    """
    anarchy_runner, anarchy_runner_pod = AnarchyRunnerPod.get_from_request(request)
    anarchy_subject_name = request.path_params['anarchy_subject_name']
    anarchy_run = AnarchyRun.get_run_assigned_to_runner_pod(anarchy_runner_pod)

    if not anarchy_run:
        logging.info(
            f"{anarchy_runner_pod} attempted to patch {anarchy_subject_name} when no "
            f"when no run should be running on this pod"
        )
        raise ResponseError.BAD_REQUEST("AnarchySubject {anarchy_subject_name} not assigned to runner")
    elif anarchy_run.subject_name != anarchy_subject_name:
        logging.info(
            f"{anarchy_runner_pod} attempted to patch {anarchy_subject_name} "
            f"when it is running {anarchy_run} for AnarchySubject {anarchy_run.subject_name}"
        )
        raise ResponseError.BAD_REQUEST(f"AnarchySubject {anarchy_subject_name} not assigned to runner")

    anarchy_subject = await AnarchySubject.get(anarchy_subject_name)

    request_data = await request.json()
    patch = request_data.get('patch')
    if not patch:
        raise ResponseError.BAD_REQUEST(f"No patch for {anarchy_subject}")

    await anarchy_subject.patch_request(patch, patch.pop('skip_update_processing', False))

    return {'success': True, 'result': anarchy_subject.definition}

@app.route('/run/subject/{anarchy_subject_name}/actions', methods=['POST'])
async def run_subject_action_post(request):
    """
    Executing AnarchyRun request to schedule action for its AnarchySubject
    """
    anarchy_runner, anarchy_runner_pod = AnarchyRunnerPod.get_from_request(request)
    anarchy_subject_name = request.path_params['anarchy_subject_name']
    anarchy_run = AnarchyRun.get_run_assigned_to_runner_pod(anarchy_runner_pod)

    if not anarchy_run:
        logging.info(
            f"{anarchy_runner_pod} attempted to create run for {anarchy_subject_name} "
            f"when no run should be running on this pod"
        )
        raise ResponseError.BAD_REQUEST("AnarchySubject {anarchy_subject_name} not assigned to runner")
    elif anarchy_run.subject_name != anarchy_subject_name:
        logging.info(
            f"{anarchy_runner_pod} attempted to create run for {anarchy_subject_name} "
            f"when it is running {anarchy_run} for AnarchySubject {anarchy_run.subject_name}"
        )
        raise ResponseError.BAD_REQUEST(f"AnarchySubject {anarchy_subject_name} not assigned to runner")

    anarchy_subject = await AnarchySubject.get(anarchy_subject_name)

    request_data = await request.json()
    anarchy_action = await anarchy_subject.schedule_action(
        action_name = request_data.get('action'),
        action_vars = request_data.get('vars', {}),
        after_timestamp = request_data.get('after'),
        cancel_actions = request_data.get('cancel', []),
        is_delete_handler = anarchy_run.is_delete_handler,
    )

    return {'success': True, 'result': anarchy_action.as_dict()}
