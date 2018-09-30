from apscheduler.jobstores.base import JobLookupError
from datetime import datetime, timedelta
from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, PickleType
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import relationship
from time import sleep

from eNMS import db, scheduler
from eNMS.base.associations import (
    task_log_rule_table,
    task_device_table,
    task_pool_table,
    task_workflow_table
)
from eNMS.base.custom_base import CustomBase
from eNMS.base.helpers import retrieve
from eNMS.base.properties import cls_to_properties


def job(task_name):
    with scheduler.app.app_context():
        task = retrieve(Task, name=task_name)
        workflow = task.workflow if task.type == 'WorkflowTask' else None
        task.job(workflow)


class Task(CustomBase):

    __tablename__ = 'Task'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    creation_time = Column(String)
    status = Column(String)
    type = Column(String)
    user_id = Column(Integer, ForeignKey('User.id'))
    user = relationship('User', back_populates='tasks')
    logs = Column(MutableDict.as_mutable(PickleType), default={})
    frequency = Column(String(120))
    start_date = Column(String)
    end_date = Column(String)
    positions = Column(MutableDict.as_mutable(PickleType), default={})
    waiting_time = Column(Integer, default=0)
    transfer_payload = Column(Boolean, default=False)
    workflows = relationship(
        'Workflow',
        secondary=task_workflow_table,
        back_populates='tasks'
    )
    log_rules = relationship(
        'LogRule',
        secondary=task_log_rule_table,
        back_populates='tasks'
    )

    __mapper_args__ = {
        'polymorphic_identity': 'Task',
        'polymorphic_on': type
    }

    def __init__(self, **kwargs):
        self.update(**kwargs)
        self.status = 'active'
        self.creation_time = str(datetime.now())
        self.is_active = True
        if 'do_not_run' not in kwargs:
            self.schedule(run_now='run_immediately' in kwargs)

    def aps_conversion(self, date):
        dt = datetime.strptime(date, '%d/%m/%Y %H:%M:%S')
        return datetime.strftime(dt, '%Y-%m-%d %H:%M:%S')

    def aps_date(self, datetype):
        date = getattr(self, datetype)
        return self.aps_conversion(date) if date else None

    def pause_task(self):
        scheduler.pause_job(self.creation_time)
        self.status = 'suspended'
        db.session.commit()

    def resume_task(self):
        scheduler.resume_job(self.creation_time)
        self.status = 'active'
        db.session.commit()

    def delete_task(self):
        try:
            scheduler.delete_job(self.creation_time)
        except JobLookupError:
            pass
        db.session.commit()

    def task_sources(self, workflow, type):
        return [
            x.source for x in self.sources
            if x.type == type and x.workflow == workflow
        ]

    def task_neighbors(self, workflow, type):
        return [
            x.destination for x in self.destinations
            if x.type == type and x.workflow == workflow
        ]

    def schedule(self, run_now=True):
        now = datetime.now() + timedelta(seconds=15)
        runtime = now if run_now else self.aps_date('start_date')
        if self.frequency:
            scheduler.add_job(
                id=self.creation_time,
                func=job,
                args=[self.name],
                trigger='interval',
                start_date=runtime,
                end_date=self.aps_date('end_date'),
                seconds=int(self.frequency),
                replace_existing=True
            )
        else:
            scheduler.add_job(
                id=self.creation_time,
                func=job,
                run_date=runtime,
                args=[self.name],
                trigger='date',
                replace_existing=True
            )

    @property
    def properties(self):
        return {p: getattr(self, p) for p in cls_to_properties['Task']}


class ServiceTask(Task):

    __tablename__ = 'ServiceTask'

    id = Column(Integer, ForeignKey('Task.id'), primary_key=True)
    service_id = Column(Integer, ForeignKey('Service.id'))
    service = relationship('Service', back_populates='tasks')
    devices = relationship(
        'Device',
        secondary=task_device_table,
        back_populates='tasks'
    )
    pools = relationship(
        'Pool',
        secondary=task_pool_table,
        back_populates='tasks'
    )

    __mapper_args__ = {
        'polymorphic_identity': 'ServiceTask',
    }

    def __init__(self, **data):
        self.service = data.pop('job')
        self.devices = data['devices']
        super().__init__(**data)

    def compute_targets(self):
        targets = set(self.devices)
        for pool in self.pools:
            targets |= set(pool.devices)
        return targets

    def job(self, workflow, payload=None):
        results = self.service.job(self, payload)
        self.logs[str(datetime.now())] = results
        db.session.commit()
        return results

    @property
    def serialized(self):
        properties = self.properties
        properties['job'] = self.service.properties if self.service else None
        properties['devices'] = [device.properties for device in self.devices]
        properties['pools'] = [pool.properties for pool in self.pools]
        return properties


class WorkflowTask(Task):

    __tablename__ = 'WorkflowTask'

    id = Column(Integer, ForeignKey('Task.id'), primary_key=True)
    workflow_id = Column(Integer, ForeignKey('Workflow.id'))
    workflow = relationship('Workflow', back_populates='task')

    __mapper_args__ = {
        'polymorphic_identity': 'WorkflowTask',
    }

    def __init__(self, **data):
        self.workflow = data.pop('job')
        super().__init__(**data)

    def job(self, workflow=None):
        runtime = str(datetime.now())
        start_task = retrieve(Task, id=self.workflow.start_task)
        if not start_task:
            return False, {runtime: 'No start task in the workflow.'}
        layer, visited = {start_task}, set()
        results = {}
        while layer:
            new_layer = set()
            for task in layer:
                visited.add(task)
                task_results = task.job(runtime, workflow, results)
                success = task_results['success']
                if task.id == self.workflow.end_task:
                    results['success'] = success
                for neighbor in task.task_neighbors(self.workflow, success):
                    if neighbor not in visited:
                        new_layer.add(neighbor)
                results[task.name] = task_results
                sleep(task.waiting_time)
            layer = new_layer
        self.logs[runtime] = results
        db.session.commit()
        return results

    @property
    def serialized(self):
        properties = {p: getattr(self, p) for p in cls_to_properties['Task']}
        properties['job'] = self.workflow.properties if self.workflow else None
        return properties
