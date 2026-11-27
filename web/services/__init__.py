"""Robot services for the dashboard. Phase 1 provides only the mock backend."""
from web.services.base import (AVAILABILITY, LIVE, SIMULATED, UNAVAILABLE, ConflictError,
                               RobotService, ServiceError, ValidationError)
from web.services.mock_robot import MockRobotService


def create_service(backend='mock', *, utilities=None, controller=None):
    if backend == 'mock':
        return MockRobotService(utilities=utilities, controller=controller)
    raise ValueError(f'Unknown robot service backend {backend!r}')


__all__ = ['AVAILABILITY', 'LIVE', 'SIMULATED', 'UNAVAILABLE', 'ConflictError', 'RobotService',
           'ServiceError', 'ValidationError', 'MockRobotService', 'create_service']
