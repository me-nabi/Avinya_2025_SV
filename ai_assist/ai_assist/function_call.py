import rclpy, time, os, cv2, base64, pygame, whisper, sys, queue, threading, wave, json, numpy as np, sounddevice as sd
from rclpy.node import Node
from geometry_msgs.msg import Twist , Point
from sensor_msgs.msg import Image
from cv_bridge import CvBridge ,  CvBridgeError
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image as PILImage
from gtts import gTTS
from io import BytesIO
from rclpy.executors import MultiThreadedExecutor

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from function_tools import first_tools, second_tools
from dock import FollowAruco

load_dotenv()


class Prompt(Node):
    def __init__(self):
        super().__init__('ai_assist_engine')
        pygame.init()
        pygame.mixer.init()

        self.publisher_ = self.create_publisher(Twist, '/cmd_vel', 10)
        self.image_sub = self.create_subscription(
            Image,
            "/camera/image_raw/uncompressed",
            self.image_callback,
            rclpy.qos.QoSPresetProfiles.SENSOR_DATA.value
        )
        self.bridge = CvBridge()
        self.cv_image = None

        self.sub_aruco = self.create_subscription(
            Point, '/detected_marker', self.listener_callback, 10)

        self.client = OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=os.getenv('API_KEY')
        )

        self.timer = self.create_timer(1.0, self.process_voice_command)

        # Whisper & Audio setup
        self.model = whisper.load_model("base", device="cpu")
        self.SAMPLE_RATE = 44100
        self.FILENAME = "recorded_audio.wav"
        self.audio_queue = queue.Queue()
        self.recording = False
        self.transcribed_text = "Press SPACE to record, release to transcribe."
        self.frames = []

        # Pygame GUI
        self.WIDTH, self.HEIGHT = 600, 400
        self.screen = pygame.display.set_mode((self.WIDTH, self.HEIGHT))
        pygame.display.set_caption("Speech Command to Robot Actions")
        self.BG_COLOR = (14, 17, 23)
        self.TEXT_COLOR = (250, 250, 250)
        self.font = pygame.font.Font(None, 30)
        self.follow_node = FollowAruco()

        self.declare_parameter("stop_distance", 5.0)  # Stop at 20 cm
        self.stop_distance = self.get_parameter('stop_distance').value

        self.target_x = 0.0
        self.target_z = 1000.0  # Start with a large distance
        self.last_received_time = time.time() - 10000


    def image_callback(self, data):
        """Receives and converts camera images."""
        try:
            self.cv_image = self.bridge.imgmsg_to_cv2(data, "bgr8")  
            self.get_logger().info("Image received.")
        except CvBridgeError as e:
            self.get_logger().error(f"CV Bridge Error: {e}")

    def draw_text(self, text):
        self.screen.fill(self.BG_COLOR)
        words = text.split(" ")
        lines, line = [], ""
        for word in words:
            test_line = line + word + " "
            if self.font.size(test_line)[0] < self.WIDTH - 40:
                line = test_line
            else:
                lines.append(line)
                line = word + " "
        lines.append(line)
        y = self.HEIGHT // 2 - (len(lines) * 15)
        for line in lines:
            text_surface = self.font.render(line, True, self.TEXT_COLOR)
            text_rect = text_surface.get_rect(center=(self.WIDTH // 2, y))
            self.screen.blit(text_surface, text_rect)
            y += 30
        pygame.display.flip()

    def audio_callback(self, indata, frames_count, time, status):
        if status:
            print("Audio Status:", status)
        self.audio_queue.put(indata.copy())

    def start_recording(self):
        self.frames = []
        self.audio_queue.queue.clear()
        self.draw_text("Listening...")
        self.recording = True

        def threaded_record():
            with sd.InputStream(samplerate=self.SAMPLE_RATE, channels=1, dtype=np.int16, callback=self.audio_callback):
                while self.recording:
                    while not self.audio_queue.empty():
                        self.frames.append(self.audio_queue.get())

        threading.Thread(target=threaded_record, daemon=True).start()

    def stop_recording(self):
        self.recording = False

    def get_gpt_response(self, prompt):
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are controlling a robot. Convert the given command into a structured JSON list of actions. Each action must have: 'action' (mov_cmd or capture), 'linear_x', and 'angular_z'. Return ONLY JSON, no extra text."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                tools=first_tools + second_tools,
            )
            content = response.choices[0].message.content.strip()
            if content.startswith("```json"):
                content = content.strip("```json").strip("```")
            actions = json.loads(content)
            return actions if isinstance(actions, list) else None
        except Exception as e:
            print(f"GPT Error: {e}")
            return None

    def move(self, distance=0.0, angle=0.0):
        linear_speed = 1.0     # meters per second
        angular_speed = 0.5    # radians per second
        msg = Twist()

        # Calculate duration
        linear_duration = abs(distance) / linear_speed if distance != 0 else 0
        angular_duration = abs(angle) / angular_speed if angle != 0 else 0

        # Move linearly
        if distance != 0:
            msg.linear.x = linear_speed if distance > 0 else -linear_speed
            msg.angular.z = 0.0
            start_time = time.time()
            while time.time() - start_time < linear_duration:
                self.publisher_.publish(msg)
                time.sleep(0.1)

        # Rotate
        if angle != 0:
            msg.linear.x = 0.0
            msg.angular.z = angular_speed if angle > 0 else -angular_speed
            start_time = time.time()
            while time.time() - start_time < angular_duration:
                self.publisher_.publish(msg)
                time.sleep(0.1)

        # Stop the robot
        self.stop_robot()


    def stop_robot(self):
        time.sleep(1)
        self.publisher_.publish(Twist())  # Stop after a short move

    def capture(self):
        image_file = self.capture_image()
        if image_file:
            self.send_image_for_description(image_file)
            return "Captured and described image."
        return "Failed to capture image."


    def capture_image(self, filename='captured_image.jpg'):
        """Captures an image from the ROS2 camera topic and resizes it before saving."""
        if self.cv_image is None:
            self.get_logger().error("No image received yet. Waiting for image...")
            rclpy.spin_once(self, timeout_sec=2.0)
            if self.cv_image is None:
                self.get_logger().error("Still no image received. Cannot capture.")
                return None

        try:
            resized_image = cv2.resize(self.cv_image, (640, 360))
            cv2.imwrite(filename, resized_image)
            self.get_logger().info(f"Image saved as {filename} (Resized to 640x360)")
            return filename
        except Exception as e:
            self.get_logger().error(f"Failed to save image: {e}")
            return None


    def encode_image(self, image_path):
        """Encodes an image to Base64 format for OpenAI API."""
        try:
            with open(image_path, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            self.get_logger().error(f"Error encoding image: {e}")
            return None

    def resize_and_compress_image(self,image_path, output_path='compressed_image.jpg', size=(320, 180), quality=80):
        """Resizes and compresses the image to reduce file size."""
        with PILImage.open(image_path) as img:
            img = img.resize(size, PILImage.LANCZOS) 
            img.save(output_path, "JPEG", quality=quality)  
        return output_path

    def send_image_for_description(self, image_file):
        """Sends the captured image to OpenAI's API for description."""
        if image_file:
            compressed_image = self.resize_and_compress_image(image_file)
        
        base64_image = self.encode_image(compressed_image)
        if not base64_image:
            return

        try:
            response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a Autonomous Mobile Robot and can describe what you see keep it short. "
                 "Also give response starting with I see and you dont have to mention if the image is blur or dim "},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image:"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            max_tokens=500,
            n=1,
            )
            description = response.choices[0].message.content.strip().lower()
            self.get_logger().info(f"Image Description: {description}")
            self.draw_text(f"Image Description: {description}")
            self.speak(description)
        except Exception as e:
            self.get_logger().error(f"Error during OpenAI API request: {e}")
        finally:
            # Clean up the image files
            for file_path in [image_file, compressed_image]:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        self.get_logger().info(f"Deleted temporary image file: {file_path}")
                except Exception as e:
                    self.get_logger().warn(f"Could not delete {file_path}: {e}")

    def speak(self,text, language='en'):
        mp3_fo = BytesIO()
        tts = gTTS(text, lang=language)
        tts.write_to_fp(mp3_fo)
        mp3_fo.seek(0)
        sound = pygame.mixer.Sound(mp3_fo)
        sound.play()
        self.wait_for_audio()

    def wait_for_audio(self):
        while pygame.mixer.get_busy():
            time.sleep(1)

    def call_function_based_on_command(self, command):
        actions = self.get_gpt_response(command)
        if actions is None:
            return "GPT returned invalid format."
        results = []
        for action in actions:
            action_type = action.get("action")
            if action_type == "mov_cmd":
                self.move(float(action.get("linear_x", 0)), float(action.get("angular_z", 0)))
                results.append(f"Moved {action.get('linear_x', 0)} meters , rotated {action.get('angular_z', 0)} radians.")
            elif action_type == "capture":
                results.append(self.capture())
            elif action_type == "docking":
                self.timer.cancel()
                self.timer = self.create_timer(0.1, self.timer_callback_aruco)
                # results.append()
        return results

    def stop_and_transcribe(self):
        self.draw_text("Processing...")
        if self.frames:
            with wave.open(self.FILENAME, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.SAMPLE_RATE)
                wf.writeframes(np.concatenate(self.frames).astype(np.int16).tobytes())

            try:
                result = self.model.transcribe(self.FILENAME)
                self.transcribed_text = result["text"].strip()
            except Exception as e:
                self.transcribed_text = "Error in transcription."
                print("Transcription error:", e)
        else:
            self.transcribed_text = "No audio captured."

        self.draw_text(f"You said: {self.transcribed_text}")
        print("Final Transcription:", self.transcribed_text)
        result = self.call_function_based_on_command(self.transcribed_text)
        print("Function Output:")
        for step, action in enumerate(result, start=1):
            print(f"  Step {step}: {action}")

    def process_voice_command(self):
        self.proc()

    def proc(self):
        running = True
        last_displayed_text = None

        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False

                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_SPACE and not self.recording:
                        self.start_recording()

                elif event.type == pygame.KEYUP:
                    if event.key == pygame.K_SPACE and self.recording:
                        self.stop_recording()
                        self.stop_and_transcribe()

            if self.transcribed_text != last_displayed_text:
                self.draw_text(self.transcribed_text)
                last_displayed_text = self.transcribed_text

            pygame.time.delay(100)

    def timer_callback_aruco(self):
        msg = Twist()

        # If marker detected recently
        if (time.time() - self.last_received_time < 1.0):
            self.get_logger().info(f'Target: {self.target_x}, Distance: {self.target_z:.2f} cm')

            if self.target_z > self.stop_distance:
                msg.linear.x = 0.3  # Move forward
            else:
                self.get_logger().info('Reached target distance. Stopping.')
                msg.linear.x = 0.0  # Stop movement
                self.timer.cancel()
                self.timer = self.create_timer(1.0, self.process_voice_command)

            msg.angular.z = -0.7 * self.target_x  # Rotate to align with marker
        else:
            self.get_logger().info('Target lost. Searching...')
            msg.angular.z = 0.5  # Rotate in place

        self.publisher_.publish(msg)

    def listener_callback(self, msg):
        self.target_x = msg.x
        self.target_z = msg.z
        self.last_received_time = time.time()

def clean_exit():
    print("\nExiting cleanly...")
    pygame.quit()
    sys.exit()

def main(args=None):
    rclpy.init(args=args)
    prompt_engine = Prompt()

    # Run the Pygame GUI in a separate thread
    pygame_thread = threading.Thread(target=prompt_engine.proc, daemon=True)
    pygame_thread.start()

    try:
        rclpy.spin(prompt_engine)
    except KeyboardInterrupt:
        print("Shutting down due to KeyboardInterrupt.")
    finally:
        prompt_engine.destroy_node()
        rclpy.shutdown()
        # clean_exit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clean_exit()

