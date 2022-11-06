from config import EyeTrackConfig
from enum import Enum
import threading
import queue
import cv2

import serial
import numpy as np
import time

WAIT_TIME = 0.1


class CameraState(Enum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2


class Camera:
    def __init__(
        self,
        config: EyeTrackConfig,
        camera_index: int,
        cancellation_event: "threading.Event",
        capture_event: "threading.Event",
        camera_status_outgoing: "queue.Queue[CameraState]",
        camera_output_outgoing: "queue.Queue",
    ):
        self.camera_status = CameraState.CONNECTING
        self.config = config
        self.camera_index = camera_index
        self.camera_address = config.capture_source
        self.camera_status_outgoing = camera_status_outgoing
        self.camera_output_outgoing = camera_output_outgoing
        self.capture_event = capture_event
        self.cancellation_event = cancellation_event
        self.current_capture_source = config.capture_source
        self.wired_camera: "cv2.VideoCapture" = None
        self.serial_connection = None
        self.error_message = "Capture source {} not found, retrying"

    def set_output_queue(self, camera_output_outgoing: "queue.Queue"):
        self.camera_output_outgoing = camera_output_outgoing

    def run(self):
        while True:
            if self.cancellation_event.is_set():
                print("Exiting capture thread")
                return
            should_push = True
            # If things aren't open, retry until they are. Don't let read requests come in any earlier
            # than this, otherwise we can deadlock ourselves.
            if (
                self.config.capture_source != None and self.config.capture_source != ""
            ):
                if (
                    self.serial_connection is None
                    and self.current_capture_source == "COM9"
                ):
                    port = self.current_capture_source
                    print(type(port))
                    self.serial_connection = start_serial_connection(port)
                elif (
                    self.wired_camera is None
                    or not self.wired_camera.isOpened()
                    or self.camera_status == CameraState.DISCONNECTED
                    or self.config.capture_source != self.current_capture_source
                ):
                    print(self.error_message.format(self.config.capture_source))
                    # This requires a wait, otherwise we can error and possible screw up the camera
                    # firmware. Fickle things.
                    if self.cancellation_event.wait(WAIT_TIME):
                        return
                    self.current_capture_source = self.config.capture_source
                    self.wired_camera = cv2.VideoCapture(self.current_capture_source)
                    should_push = False
            else:
                # We don't have a capture source to try yet, wait for one to show up in the GUI.
                if self.cancellation_event.wait(WAIT_TIME):
                    self.camera_status = CameraState.DISCONNECTED
                    return
            # Assuming we can access our capture source, wait for another thread to request a capture.
            # Cycle every so often to see if our cancellation token has fired. This basically uses a
            # python event as a contextless, resettable one-shot channel.
            if should_push and not self.capture_event.wait(timeout=0.02):
                continue

            self.get_wired_camera_picture(should_push)
            if not should_push:
                # if we get all the way down here, consider ourselves connected
                self.camera_status = CameraState.CONNECTED

    def get_wired_camera_picture(self, should_push):
        try:
            ret, image = self.wired_camera.read()
            if not ret:
                self.wired_camera.set(cv2.CAP_PROP_POS_FRAMES, 0)
                raise RuntimeError("Problem while getting frame")
            frame_number = self.wired_camera.get(cv2.CAP_PROP_POS_FRAMES)
            fps = self.wired_camera.get(cv2.CAP_PROP_FPS)
            if should_push:
                self.push_image_to_queue(image, frame_number, fps)
        except:
            print(
                "Capture source problem, assuming camera disconnected, waiting for reconnect."
            )
            self.camera_status = CameraState.DISCONNECTED
            pass

    def get_serial_camera_picture(self, should_push, serialInst: serial):
        start = time.time()
        try:
            if self.serial_connection.in_waiting:
                print("Serial")
                bytes += self.serial_connection.read(4096)  # Read in initial bytes

                a = bytes.find(b'\xff\xd8') # Find start byte for jpeg image
                b = bytes.find(b'\xff\xd9') # Fine end byte for jpeg image

                # If the first found end byte is before the start byte, keep reading in serial
                # data and discarding the old data until the start byte is before the end byte
                # - I believe this may be a poor implentation and discards useful data but it
                # requires more testing to verify this.
                while a > b:
                    print("less")
                    bytes = bytes[a:]
                    a = bytes.find(b'\xff\xd8')
                    b = bytes.find(b'\xff\xd9')
                    if a == -1 or b == -1:
                        bytes += self.serial_connection.read(2048)
                    #print(a)
                    #print(b)

                #print(a)
                #print(b)
                if a != -1 and b != -1: # If there is jpeg data
                    #size = len(bytes)
                    jpg = bytes[a:b+2]  # Create the string of bytes for the current jpeg
                    bytes = bytes[b+2:] # Clear the buffer until the end of our current jpeg
                    print(len(jpg))
            
                    if jpg:
                        # Create jpeg frame from byte string
                        image = cv2.imdecode(np.fromstring(jpg, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
                        frame_number = 1 # I don't think this is good
                        fps = 1/(time.time() - start)   # Calculate FPS - This could use a better implementation
                        if should_push:
                            self.push_image_to_queue(image, frame_number, fps)


        except:
            print(
            "Serial capture source problem, assuming camera disconnected, waiting for reconnect.")
            self.camera_status = CameraState.DISCONNECTED
            pass

    # def start_serial_connection(self, port):
    #     serialInst = serial.Serial()
    #     print("setting baudrate")
    #     serialInst.baudrate = 2000000
    #     print("baudrate set")

    #     serialInst.port = port
    #     serialInst.setDTR(False)
    #     serialInst.setRTS(False)

    #     if not serialInst.isOpen():
    #         print("COM Port already open, there may be another application interfering with the connection")
    #         return False
        
    #     serialInst.open()
    #     print("port open")
    #     return serialInst

    def push_image_to_queue(self, image, frame_number, fps):
        # If there's backpressure, just yell. We really shouldn't have this unless we start getting
        # some sort of capture event conflict though.
        qsize = self.camera_output_outgoing.qsize()
        if qsize > 1:
            print(
                f"CAPTURE QUEUE BACKPRESSURE OF {qsize}. CHECK FOR CRASH OR TIMING ISSUES IN ALGORITHM."
            )
        self.camera_output_outgoing.put((image, frame_number, fps))
        self.capture_event.clear()

def start_serial_connection(port):
    serialInst = serial.Serial()
    print("setting baudrate")
    serialInst.baudrate = 2000000
    print("baudrate set")

    serialInst.port = port
    serialInst.setDTR(False)
    serialInst.setRTS(False)

    if not serialInst.isOpen():
        print("COM Port already open, there may be another application interfering with the connection")
        return False
        
    serialInst.open()
    print("port open")
    return serialInst
