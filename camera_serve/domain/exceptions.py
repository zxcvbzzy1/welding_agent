class CameraServiceError(Exception):
    """Base class for errors that can be exposed by the HTTP adapter."""


class CameraNotConnectedError(CameraServiceError):
    pass


class CameraNotFoundError(CameraServiceError):
    pass


class CameraAlreadyConnectedError(CameraServiceError):
    pass


class CameraSdkError(CameraServiceError):
    def __init__(self, operation: str, code: int, description: str) -> None:
        self.operation = operation
        self.code = code
        self.description = description
        super().__init__(f"{operation} failed ({code}): {description}")


class InvalidTransformationError(CameraServiceError):
    pass


class CaptureFileNotFoundError(CameraServiceError):
    pass


class RtcCapacityError(CameraServiceError):
    pass

