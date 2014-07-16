#!/usr/bin/env python
import roslib; roslib.load_manifest('ur_driver')
import time, sys, threading, math
import copy
import datetime
import socket, select
import struct
import traceback, code
import optparse
import SocketServer
from BeautifulSoup import BeautifulSoup

import rospy
# import actionlib
from sensor_msgs.msg import JointState
from control_msgs.msg import FollowJointTrajectoryAction
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from geometry_msgs.msg import Pose
from deserialize import RobotState, RobotMode

import tf, PyKDL
import tf_conversions as tf_c

import ur_driver
from ur_driver.srv import *

prevent_programming = False

# Joint offsets, pulled from calibration information stored in the URDF
#
# { "joint_name" : offset }
#
# q_actual = q_from_driver + offset
joint_offsets = {}

PORT=30002
REVERSE_PORT = 50001

MSG_OUT = 1
MSG_QUIT = 2
MSG_JOINT_STATES = 3
MSG_MOVEJ = 4
MSG_WAYPOINT_FINISHED = 5
MSG_STOPJ = 6
MSG_SERVOJ = 7
MSG_MOVEL = 8
MSG_TCP_STATE = 9
MSG_SERVOC = 10
MSG_FREEDRIVE = 11
MULT_jointstate = 10000.0
MULT_time = 1000000.0
MULT_blend = 1000.0

VAL_TRUE = 1
VAL_FALSE = 0

JOINT_NAMES = ['shoulder_pan_joint', 'shoulder_lift_joint', 'elbow_joint',
               'wrist_1_joint', 'wrist_2_joint', 'wrist_3_joint']

Q1 = [2.2,0,-1.57,0,0,0]
Q2 = [1.5,0,-1.57,0,0,0]
Q3 = [1.5,-0.2,-1.57,0,0,0]

free_drive = False
  

connected_robot = None
connected_robot_lock = threading.Lock()
connected_robot_cond = threading.Condition(connected_robot_lock)
pub_joint_states = rospy.Publisher('joint_states', JointState)
#dump_state = open('dump_state', 'wb')

class EOF(Exception): pass

def dumpstacks():
    id2name = dict([(th.ident, th.name) for th in threading.enumerate()])
    code = []
    for threadId, stack in sys._current_frames().items():
        code.append("\n# Thread: %s(%d)" % (id2name.get(threadId,""), threadId))
        for filename, lineno, name, line in traceback.extract_stack(stack):
            code.append('File: "%s", line %d, in %s' % (filename, lineno, name))
            if line:
                code.append("  %s" % (line.strip()))
    print "\n".join(code)

def log(s):
    print "[%s] %s" % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'), s)


RESET_PROGRAM = '''def resetProg():
  sleep(0.0)
end
'''

FREE_DRIVE_PROGRAM = '''def freedriveProg():
  set robotmode freedrive
end
'''
    
class UR5Connection(object):
    TIMEOUT = 1.0
    
    DISCONNECTED = 0
    CONNECTED = 1
    READY_TO_PROGRAM = 2
    EXECUTING = 3
    FREE_DRIVE = 4
    
    def __init__(self, hostname, port, program):
        self.__thread = None
        self.__sock = None
        self.robot_state = self.DISCONNECTED
        self.hostname = hostname
        self.port = port
        self.program = program
        self.last_state = None

    def connect(self):
        if self.__sock:
            self.disconnect()
        self.__buf = ""
        self.robot_state = self.CONNECTED
        self.__sock = socket.create_connection((self.hostname, self.port))
        self.__keep_running = True
        self.__thread = threading.Thread(name="UR5Connection", target=self.__run)
        self.__thread.daemon = True
        self.__thread.start()

    def send_program(self):
        global prevent_programming
        if prevent_programming:
            rospy.loginfo("Programming is currently prevented")
            return
        assert self.robot_state in [self.READY_TO_PROGRAM, self.EXECUTING]
        rospy.loginfo("Programming the robot at %s" % self.hostname)
        self.__sock.sendall(self.program)
        self.robot_state = self.EXECUTING

    def send_reset_program(self):
        self.__sock.sendall(RESET_PROGRAM)
        self.robot_state = self.READY_TO_PROGRAM

    def send_free_drive_program(self):
        self.__sock.sendall(FREE_DRIVE_PROGRAM.strip())
        self.robot_state = self.FREE_DRIVE
        
    def disconnect(self):
        if self.__thread:
            self.__keep_running = False
            self.__thread.join()
            self.__thread = None
        if self.__sock:
            self.__sock.close()
            self.__sock = None
        self.last_state = None
        self.robot_state = self.DISCONNECTED

    def ready_to_program(self):
        return self.robot_state in [self.READY_TO_PROGRAM, self.EXECUTING]

    def __trigger_disconnected(self):
        log("Robot disconnected")
        self.robot_state = self.DISCONNECTED
    def __trigger_ready_to_program(self):
        rospy.loginfo("Robot ready to program")
    def __trigger_halted(self):
        log("Halted")

    def __on_packet(self, buf):
        state = RobotState.unpack(buf)
        self.last_state = state
        #import deserialize; deserialize.pstate(self.last_state)

        #log("Packet.  Mode=%s" % state.robot_mode_data.robot_mode)

        if not state.robot_mode_data.real_robot_enabled:
            rospy.logfatal("Real robot is no longer enabled.  Driver is fuxored")
            time.sleep(2)
            sys.exit(1)

        # If the urscript program is not executing, then the driver
        # needs to publish joint states using information from the
        # robot state packet.
        if self.robot_state != self.EXECUTING:
            msg = JointState()
            msg.header.stamp = rospy.get_rostime()
            msg.header.frame_id = "From binary state data"
            msg.name = joint_names
            msg.position = [0.0] * 6
            for i, jd in enumerate(state.joint_data):
                msg.position[i] = jd.q_actual + joint_offsets.get(joint_names[i], 0.0)
            msg.velocity = [jd.qd_actual for jd in state.joint_data]
            msg.effort = [0]*6
            pub_joint_states.publish(msg)
            self.last_joint_states = msg

        # Updates the state machine that determines whether we can program the robot.
        can_execute = (state.robot_mode_data.robot_mode in [RobotMode.READY, RobotMode.RUNNING])
        if self.robot_state == self.CONNECTED:
            if can_execute:
                self.__trigger_ready_to_program()
                self.robot_state = self.READY_TO_PROGRAM
        elif self.robot_state == self.READY_TO_PROGRAM:
            if not can_execute:
                self.robot_state = self.CONNECTED
        elif self.robot_state == self.EXECUTING:
            if not can_execute:
                self.__trigger_halted()
                self.robot_state = self.CONNECTED

        # Report on any unknown packet types that were received
        if len(state.unknown_ptypes) > 0:
            state.unknown_ptypes.sort()
            s_unknown_ptypes = [str(ptype) for ptype in state.unknown_ptypes]
            self.throttle_warn_unknown(1.0, "Ignoring unknown pkt type(s): %s. "
                          "Please report." % ", ".join(s_unknown_ptypes))

    def throttle_warn_unknown(self, period, msg):
        self.__dict__.setdefault('_last_hit', 0.0)
        # this only works for a single caller
        if (self._last_hit + period) <= rospy.get_time():
            self._last_hit = rospy.get_time()
            rospy.logwarn(msg)

    def __run(self):
        while self.__keep_running:
            r, _, _ = select.select([self.__sock], [], [], self.TIMEOUT)
            if r:
                more = self.__sock.recv(4096)
                if more:
                    self.__buf = self.__buf + more

                    # Attempts to extract a packet
                    packet_length, ptype = struct.unpack_from("!IB", self.__buf)
                    if len(self.__buf) >= packet_length:
                        packet, self.__buf = self.__buf[:packet_length], self.__buf[packet_length:]
                        self.__on_packet(packet)
                else:
                    self.__trigger_disconnected()
                    self.__keep_running = False
                    
            else:
                self.__trigger_disconnected()
                self.__keep_running = False


def setConnectedRobot(r):
    global connected_robot, connected_robot_lock
    with connected_robot_lock:
        connected_robot = r
        connected_robot_cond.notify()

def getConnectedRobot(wait=False, timeout=-1):
    started = time.time()
    with connected_robot_lock:
        if wait:
            while not connected_robot:
                if timeout >= 0 and time.time() > started + timeout:
                    break
                connected_robot_cond.wait(0.2)
        return connected_robot

# Receives messages from the robot over the socket
class CommanderTCPHandler(SocketServer.BaseRequestHandler):

    def recv_more(self):
        while True:
            r, _, _ = select.select([self.request], [], [], 0.2)
            if r:
                more = self.request.recv(4096)
                if not more:
                    raise EOF("EOF on recv")
                return more
            else:
                now = rospy.get_rostime()
                if self.last_joint_states and \
                        self.last_joint_states.header.stamp < now - rospy.Duration(1.0):
                    rospy.logerr("Stopped hearing from robot (last heard %.3f sec ago).  Disconnected" % \
                                     (now - self.last_joint_states.header.stamp).to_sec())
                    raise EOF()

    def handle(self):
        self.socket_lock = threading.Lock()
        self.last_joint_states = None
        self.last_tcp_state = None
        setConnectedRobot(self)
        print "Handling a request"
        try:
            buf = self.recv_more()
            if not buf: return

            while True:
                #print "Buf:", [ord(b) for b in buf]

                # Unpacks the message type
                mtype = struct.unpack_from("!i", buf, 0)[0]
                buf = buf[4:]
                #print "Message type:", mtype

                if mtype == MSG_OUT:
                    # Unpacks string message, terminated by tilde
                    i = buf.find("~")
                    while i < 0:
                        buf = buf + self.recv_more()
                        i = buf.find("~")
                        if len(buf) > 2000:
                            raise Exception("Probably forgot to terminate a string: %s..." % buf[:150])
                    s, buf = buf[:i], buf[i+1:]
                    log("Out: %s" % s)

                elif mtype == MSG_JOINT_STATES:
                    while len(buf) < 3*(6*4):
                        buf = buf + self.recv_more()
                    state_mult = struct.unpack_from("!%ii" % (3*6), buf, 0)
                    buf = buf[3*6*4:]
                    state = [s / MULT_jointstate for s in state_mult]

                    msg = JointState()
                    msg.header.stamp = rospy.get_rostime()
                    msg.name = joint_names
                    msg.position = [0.0] * 6
                    for i, q_meas in enumerate(state[:6]):
                        msg.position[i] = q_meas + joint_offsets.get(joint_names[i], 0.0)
                    msg.velocity = state[6:12]
                    msg.effort = state[12:18]
                    self.last_joint_states = msg
                    # rospy.logwarn(str(msg))
                    pub_joint_states.publish(msg)
                elif mtype == MSG_TCP_STATE:
                    while len(buf) < 1*(6*4):
                        buf = buf + self.recv_more()
                    state_mult = struct.unpack_from("!%ii" % (1*6), buf, 0)
                    buf = buf[1*6*4:]
                    state = [s / MULT_jointstate for s in state_mult]
                    self.last_tcp_state_as_euler = state

                    T = PyKDL.Frame()
                    T.p = PyKDL.Vector(state[0],state[1],state[2])
                    T.M = PyKDL.Rotation.RPY(state[3],state[4],state[5])
                    # print state
                    # print T.M.GetRPY()
                    tcp_pose = tf_c.toMsg(T)
                    # rospy.logwarn(str(tcp_pose))
                    self.last_tcp_state = tcp_pose
                elif mtype == MSG_QUIT:
                    print "Quitting"
                    raise EOF("Received quit")
                elif mtype == MSG_WAYPOINT_FINISHED:
                    while len(buf) < 4:
                        buf = buf + self.recv_more()
                    waypoint_id = struct.unpack_from("!i", buf, 0)[0]
                    buf = buf[4:]
                    print "Waypoint finished (not handled)"
                else:
                    raise Exception("Unknown message type: %i" % mtype)

                if not buf:
                    buf = buf + self.recv_more()
        except EOF, ex:
            print "Connection closed (command):", ex
            setConnectedRobot(None)

    def send_quit(self):
        with self.socket_lock:
            self.request.send(struct.pack("!i", MSG_QUIT))
            
    def send_servoj(self, waypoint_id, q_actual, t):
        assert(len(q_actual) == 6)
        q_robot = [0.0] * 6
        for i, q in enumerate(q_actual):
            q_robot[i] = q - joint_offsets.get(joint_names[i], 0.0)
        params = [MSG_SERVOJ, waypoint_id] + \
                 [MULT_jointstate * qq for qq in q_robot] + \
                 [MULT_time * t]
        buf = struct.pack("!%ii" % len(params), *params)
        with self.socket_lock:
            self.request.send(buf)

    def send_servoc(self, waypoint_id, pose):
        ''' 
        @param pose: 6DOF EULER Pose [x,y,z,r,p,y]
        '''
        assert(len(q_actual) == 6)
        pose_robot = pose
        params = [MSG_SERVOC, waypoint_id] + [MULT_jointstate * pp for pp in pose_robot] + [MULT_time * t]
        buf = struct.pack("!%ii" % len(params), *params)
        with self.socket_lock:
            self.request.send(buf)

    def send_movel(self, waypoint_id, pose):
        ''' 
        @param pose: 6DOF EULER Pose [x,y,z,r,p,y]
        '''
        assert(len(q_actual) == 6)
        pose_robot = pose
        params = [MSG_MOVEL, waypoint_id] + [MULT_jointstate * pp for pp in pose_robot]
        buf = struct.pack("!%ii" % len(params), *params)
        with self.socket_lock:
            self.request.send(buf)

    # def send_freedrive(self,free_drive_active):
    #     if free_drive_active:
    #         val = VAL_TRUE
    #     else:
    #         val = VAL_FALSE
    #     params = [MSG_FREEDRIVE, val]
    #     buf = struct.pack("!%ii" % len(params), *params)
    #     with self.socket_lock:
    #         self.request.send(buf)

    def send_stopj(self):
        with self.socket_lock:
            self.request.send(struct.pack("!i", MSG_STOPJ))

    def set_waypoint_finished_cb(self, cb):
        self.waypoint_finished_cb = cb

    # Returns the last JointState message sent out
    def get_joint_states(self):
        return self.last_joint_states

    def get_tcp_state(self):
        return self.last_tcp_state

    def get_tcp_state_as_euler(self):
        return self.last_tcp_state_as_euler
    
# UTILITY FUNCTIONS ------------------------------------------------------------
def load_joint_offsets(joint_names):
    robot_description = rospy.get_param("robot_description")
    soup = BeautifulSoup(robot_description)
    
    result = {}
    for joint in joint_names:
        try:
            joint_elt = soup.find('joint', attrs={'name': joint})
            calibration_offset = float(joint_elt.calibration_offset["value"])
            result[joint] = calibration_offset
        except Exception, ex:
            rospy.logwarn("No calibration offset for joint \"%s\"" % joint)
    return result
    
def get_my_ip(robot_ip, port):
    s = socket.create_connection((robot_ip, port))
    tmp = s.getsockname()[0]
    s.close()
    return tmp

class TCPServer(SocketServer.TCPServer):
    allow_reuse_address = True  # Allows the program to restart gracefully on crash
    timeout = 5

# Waits until all threads have completed.  Allows KeyboardInterrupt to occur
def joinAll(threads):
    while any(t.isAlive() for t in threads):
        for t in threads:
            t.join(0.2)

class UR5ServoDriver(object):
    IDLE = 0
    SERVO_ACTIVE = 1
    FREE_DRIVE = 2

    def __init__(self):
        self.robot = None
        self.pose_sub = rospy.Subscriber("/ur5_command_pose",Pose,self.pose_cb)
        self.__mode = self.IDLE
        movel_srv = rospy.Service('/ur_driver/movel', ur_driver.srv.movel, self.service_movel)
        get_tcp_pose_srv = rospy.Service('/ur_driver/get_tcp_pose', ur_driver.srv.get_tcp_pose, self.service_get_tcp_pose)
        rospy.logwarn('UR5 SERVO INTERFACES SET UP')

    def set_up_robot(self,robot):
        self.robot = robot
        self.init_joint_states = self.robot.get_joint_states()   
        self.init_tcp_state = self.robot.get_tcp_state() 

    def pose_cb(self,msg):
        pose = msg.data
        # try:
        #     self.robot.send_servoj(999, setpoint.positions)
        # except socket.error:
        #     pass

    def service_movel(self,data):
        if self.robot:
            target = data.target # target is a Pose
            T = tf_c.fromMsg(target)
            rot = T.M.GetRPY()
            pose = list(T.p) + [rot[0], rot[1], rot[2]]
            return str(pose)
            # try:
            #     self.robot.send_movel(999, pose)
            # except socket.error:
            #     pass

    def service_get_tcp_pose(self,data):
        if self.robot:
            resp = ur_driver.srv.get_tcp_poseResponse()
            resp.current_pose = self.robot.get_tcp_state()
            resp.current_euler = self.robot.get_tcp_state_as_euler()
            return resp

# GLOBAL SERVICES --------------------------------------------------------------
def service_free_drive(data):
    global free_drive
    active = data.active
    if active == False:
        free_drive = False
        # print free_drive
        return 'set freedrive false'
    else:
        free_drive = True
        # print free_drive
        return 'set freedrive true'
    pass


class ServoDriver(object):
    def __init__(self):

        rospy.init_node('ur_servo_driver', disable_signals=True)
        if rospy.get_param("use_sim_time", False):
            rospy.logwarn("use_sim_time is set!!!")

        # Set up programming environment
        global prevent_programming
        prevent_programming = rospy.get_param("prevent_programming", False)
        prefix = rospy.get_param("~prefix", "")
        print "Setting prefix to %s" % prefix
        global joint_names
        joint_names = [prefix + name for name in JOINT_NAMES]

        # Parses command line arguments
        parser = optparse.OptionParser(usage="usage: %prog robot_hostname")
        (options, args) = parser.parse_args(rospy.myargv()[1:])
        if len(args) != 1:
            parser.error("You must specify the robot hostname")
        robot_hostname = args[0]

        # Reads the calibrated joint offsets from the URDF
        global joint_offsets
        joint_offsets = load_joint_offsets(joint_names)
        rospy.logerr("Loaded calibration offsets: %s" % joint_offsets)

        # Reads the maximum velocity
        global max_velocity
        max_velocity = rospy.get_param("~max_velocity", 2.0)

        # Sets up the server for the robot to connect to
        server = TCPServer(("", 50001), CommanderTCPHandler)
        thread_commander = threading.Thread(name="CommanderHandler", target=server.serve_forever)
        thread_commander.daemon = True
        thread_commander.start()

        # Send servo program
        with open(roslib.packages.get_pkg_dir('ur_driver') + '/prog_servo') as fin:
            program = fin.read() % {"driver_hostname": get_my_ip(robot_hostname, PORT)}
        connection = UR5Connection(robot_hostname, PORT, program)
        connection.connect()
        connection.send_reset_program()
        
        servo_driver = None

        global free_drive
        print 'freedrive set to false'
        free_drive_enabled = False
        free_drive_srv = rospy.Service('/ur_driver/free_drive', ur_driver.srv.free_drive,service_free_drive)


        servo_driver = UR5ServoDriver()
        rospy.logwarn('SERVO DRIVER INTERFACES RUNNING')
        
        # action_server = None
        try:
            while not rospy.is_shutdown():
                # Checks for disconnect
                if getConnectedRobot(wait=False):
                    time.sleep(0.2)
                    prevent_programming = rospy.get_param("prevent_programming", False)
                    if prevent_programming:
                        print "Programming now prevented"
                        connection.send_reset_program()
                    if free_drive_enabled == True:
                        # print 'freedrive currently ' + str(free_drive)
                        if free_drive == False:
                            connection.send_reset_program()
                            # connection.send_program()
                            free_drive_enabled = False
                            rospy.logwarn('ROBOT FREEDRIVE DISABLED')
                    elif free_drive_enabled == False:
                        # print 'freedrive currently ' + str(free_drive)
                        if free_drive == True:
                            # connection.send_reset_program()
                            connection.send_free_drive_program()
                            free_drive_enabled = True
                            rospy.logwarn('ROBOT FREEDRIVE ENABLED')
                else:
                    print "Disconnected.  Reconnecting"
                    # if action_server:
                    #     action_server.set_robot(None)

                    rospy.loginfo("Programming the robot")
                    while True:
                        # Sends the program to the robot
                        while not connection.ready_to_program():
                            print "Waiting to program"
                            time.sleep(1.0)
                        prevent_programming = rospy.get_param("prevent_programming", False)
                        connection.send_program()
                        rospy.loginfo('Sent Program')
                        connected_robot = getConnectedRobot(wait=True, timeout=1.0)
                        rospy.loginfo(str(connected_robot))
                        if connected_robot:
                            break
                    rospy.loginfo("Robot connected")

                    servo_driver.set_up_robot(connected_robot)
                    rospy.logwarn('UR5 CONNECTED TO SERVO DRIVER')
                    # if action_server:
                    #     action_server.set_robot(r)
                    # else:
                    #     action_server = UR5TrajectoryFollower(r, rospy.Duration(1.0))
                    #     action_server.start()

        except KeyboardInterrupt:
            try:
                r = getConnectedRobot(wait=False)
                rospy.signal_shutdown("KeyboardInterrupt")
                if r: r.send_quit()
            except:
                pass
            raise

if __name__ == '__main__':
    S = ServoDriver()
