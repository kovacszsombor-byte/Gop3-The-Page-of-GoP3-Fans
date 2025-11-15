# server/app.rb
#
# GOP3 Fan Page - Ruby (Sinatra) backend example
#
# Purpose:
# - Provide endpoints to receive contact form submissions from the front-end.
# - Send e-mails via SMTP (configured from environment variables).
# - Accept JSON and multipart/form-data (with optional file attachments).
# - Include logging, basic rate-limiting, validation, retry logic and health check.
# - This file is intentionally verbose and commented to meet the ">=300 lines" request.
#
# Usage:
# 1. Install Ruby and bundler, then create a Gemfile with sinatra, pony, rack-cors
#    Example Gemfile:
#      source 'https://rubygems.org'
#      gem 'sinatra'
#      gem 'pony'
#      gem 'rack-cors'
#
# 2. Install gems:
#      bundle install
#
# 3. Set environment variables:
#      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, SMTP_TO
#
# 4. Run:
#      ruby server/app.rb -o 0.0.0.0 -p 4567
#
# Security notes:
# - Never commit SMTP credentials.
# - For production use, run behind HTTPS and use a proper email provider and anti-spam measures.
require 'sinatra'
require 'json'
require 'pony'
require 'logger'
require 'securerandom'
require 'time'
require 'thread'
require 'rack/cors'
require 'tempfile'

# ----------------------------
# Configuration & constants
# ----------------------------
APP_NAME = "GOP3 Fan Page Backend (Sinatra)"
VERSION = "1.0.0"

# Logging
LOGGER = Logger.new($stdout)
LOGGER.level = Logger::INFO

# SMTP configuration from ENV
SMTP_HOST = ENV['SMTP_HOST'] || ''
SMTP_PORT = (ENV['SMTP_PORT'] || '587').to_i
SMTP_USER = ENV['SMTP_USER'] || ''
SMTP_PASS = ENV['SMTP_PASS'] || ''
SMTP_FROM = ENV['SMTP_FROM'] || 'gop3-fan@example.com'
SMTP_TO = ENV['SMTP_TO'] || SMTP_FROM

# Limit settings
MAX_ATTACHMENT_SIZE = 8 * 1024 * 1024 # 8 MiB
ALLOWED_EXT = %w[png jpg jpeg gif pdf txt md]
RATE_LIMIT_WINDOW = 60 # seconds
RATE_LIMIT_MAX = 6 # per IP per window

# Upload folder
UPLOAD_FOLDER = ENV['UPLOAD_FOLDER'] || "/tmp/gop3_uploads"
Dir.mkdir(UPLOAD_FOLDER) unless Dir.exist?(UPLOAD_FOLDER)

# Simple in-memory rate limiter (not cluster-safe)
$rate_lock = Mutex.new
$rate_store = {} # ip => { count: int, window_start: int }

# CORS middleware (for dev). In production restrict origins.
use Rack::Cors do
  allow do
    origins '*'
    resource '*', headers: :any, methods: [:get, :post, :options]
  end
end

# ----------------------------
# Helpers
# ----------------------------
def now_epoch
  Time.now.to_i
end

def rate_limited?(ip)
  $rate_lock.synchronize do
    data = $rate_store[ip]
    now = now_epoch
    if data.nil?
      $rate_store[ip] = { count: 1, window_start: now }
      return false
    end
    if now - data[:window_start] > RATE_LIMIT_WINDOW
      $rate_store[ip] = { count: 1, window_start: now }
      return false
    end
    if data[:count] >= RATE_LIMIT_MAX
      LOGGER.warn("Rate limiter: ip #{ip} exceeded limit")
      return true
    end
    data[:count] += 1
    return false
  end
end

def allowed_filename?(filename)
  return false unless filename && filename.include?('.')
  ext = filename.split('.').last.downcase
  ALLOWED_EXT.include?(ext)
end

def sanitize_string(v)
  return "" if v.nil?
  v.to_s.strip
end

def build_email_body(hash)
  lines = []
  lines << "New message from GOP3 Fan Page contact form"
  lines << ""
  lines << "Sent at: #{Time.now.utc.iso8601}Z"
  lines << ""
  lines << "----"
  lines << "Name: #{sanitize_string(hash['name'])}" if hash['name']
  lines << "Email: #{sanitize_string(hash['email'])}" if hash['email']
  lines << "Subject: #{sanitize_string(hash['subject'])}" if hash['subject']
  lines << ""
  lines << "Message:"
  lines << (sanitize_string(hash['message']) == "" ? "(no message)" : sanitize_string(hash['message']))
  lines << ""
  lines << "----"
  if hash['meta']
    lines << "Meta:"
    lines << JSON.pretty_generate(hash['meta']) rescue hash['meta'].to_s
  end
  lines.join("\n")
end

def send_email(subject:, body:, reply_to: nil, attachments: [])
  # Build Pony options and send the email. Pony wraps Net::SMTP or sendmail.
  options = {
    via: :smtp,
    via_options: {
      address: SMTP_HOST,
      port: SMTP_PORT,
      enable_starttls_auto: true,
      user_name: SMTP_USER,
      password: SMTP_PASS,
      authentication: :plain, # adjust if needed
      domain: ENV['SMTP_DOMAIN'] || 'localhost.localdomain'
    }
  }

  mail_opts = {
    to: SMTP_TO,
    from: SMTP_FROM,
    subject: subject,
    body: body
  }

  mail_opts[:reply_to] = reply_to if reply_to

  # Attachments: array of {filename:, tempfile:}
  unless attachments.empty?
    # Pony accepts attachments as filename => File or String
    attach_hash = {}
    attachments.each do |att|
      fname = att[:filename]
      content = att[:content] # binary string
      # Write to a Tempfile and pass path
      tf = Tempfile.new(["gop3-att-", File.extname(fname)])
      tf.binmode
      tf.write(content)
      tf.flush
      tf.rewind
      attach_hash[fname] = tf.path
      # Note: tempfiles will be GC'd/closed after process exits; we will unlink manually later
    end
    mail_opts[:attachments] = attach_hash
  end

  # Try with a small retry loop
  attempts = 0
  last_err = nil
  begin
    attempts += 1
    LOGGER.info("Attempting to send email (attempt #{attempts}) to #{SMTP_TO}")
    Pony.mail(mail_opts.merge(options))
    LOGGER.info("Email sent successfully to #{SMTP_TO}")
  rescue => e
    last_err = e
    LOGGER.error("Error sending email via Pony: #{e.class} #{e.message}")
    if attempts < 3
      sleep(1 * (2 ** (attempts - 1)))
      retry
    else
      raise e
    end
  ensure
    # cleanup tempfiles if present
    if mail_opts[:attachments]
      mail_opts[:attachments].each_value do |path|
        begin
          File.unlink(path) if File.exist?(path)
        rescue => cleanup_err
          LOGGER.warn("Failed to cleanup attachment tempfile #{path}: #{cleanup_err}")
        end
      end
    end
  end
end

# ----------------------------
# Routes
# ----------------------------
get '/' do
  content_type :json
  { status: 'ok', message: "#{APP_NAME} v#{VERSION}" }.to_json
end

get '/health' do
  content_type :json
  { status: 'healthy', time: Time.now.utc.iso8601 }.to_json
end

post '/send-email' do
  request.body.rewind
  ip = request.env['HTTP_X_FORWARDED_FOR'] || request.ip || 'unknown'
  LOGGER.info("POST /send-email from #{ip}")

  if rate_limited?(ip)
    LOGGER.warn("Rejecting request from #{ip} due to rate limit")
    status 429
    return { error: 'Too many requests. Try again later.' }.to_json
  end

  payload = {}
  attachments = []

  content_type = request.content_type.to_s
  if content_type.start_with?('application/json')
    begin
      payload = JSON.parse(request.body.read)
    rescue => e
      LOGGER.warn("Invalid JSON payload: #{e}")
      status 400
      return { error: 'Invalid JSON payload' }.to_json
    end
  else
    # Assume form-data or urlencoded
    payload['name'] = params['name']
    payload['email'] = params['email']
    payload['subject'] = params['subject']
    payload['message'] = params['message']
    if params['meta']
      begin
        payload['meta'] = JSON.parse(params['meta'])
      rescue
        payload['meta'] = params['meta']
      end
    end

    # Handle file uploads via params (Sinatra puts uploaded files in params as Hash)
    params.each do |k, v|
      if v.respond_to?(:[] ) && v[:tempfile] && v[:filename]
        fname = v[:filename]
        if !allowed_filename?(fname)
          LOGGER.warn("Rejected attachment with disallowed extension: #{fname}")
          status 400
          return { error: "Attachment type not allowed: #{fname}" }.to_json
        end
        # Check size
        v[:tempfile].seek(0, IO::SEEK_END)
        size = v[:tempfile].tell
        v[:tempfile].rewind
        if size > MAX_ATTACHMENT_SIZE
          LOGGER.warn("Rejected attachment too large: #{fname} (#{size} bytes)")
          status 400
          return { error: "Attachment too large: #{fname}" }.to_json
        end
        content = v[:tempfile].read
        attachments << { filename: fname, content: content }
        LOGGER.info("Received attachment #{fname} (#{size} bytes)")
      end
    end
  end

  # Validate required fields
  name = sanitize_string(payload['name'])
  email = sanitize_string(payload['email'])
  subject = sanitize_string(payload['subject'] || 'GOP3 Fan Message')
  message = sanitize_string(payload['message'])

  if name.empty? || email.empty? || message.empty?
    LOGGER.debug("Validation failed: missing required fields")
    status 422
    return { error: 'Missing required fields: name, email, and message are required' }.to_json
  end

  body = build_email_body({ 'name' => name, 'email' => email, 'subject' => subject, 'message' => message, 'meta' => payload['meta'] })

  begin
    send_email(subject: "[GOP3 Fan] #{subject} (from #{name})", body: body, reply_to: email, attachments: attachments)
    status 200
    return { message: 'Email sent successfully' }.to_json
  rescue => e
    LOGGER.error("Failed to send email: #{e.class} #{e.message}")
    status 500
    return { error: 'Failed to send email', detail: e.message }.to_json
  end
end

# Optional small HTML test page (for manual testing)
get '/_test_page' do
  <<~HTML
  <!doctype html>
  <html>
    <head><meta charset="utf-8"><title>#{APP_NAME} - Test</title></head>
    <body>
      <h1>#{APP_NAME} v#{VERSION}</h1>
      <form method="post" action="/send-email" enctype="multipart/form-data">
        <label>Name: <input name="name" required></label><br/>
        <label>Email: <input name="email" required></label><br/>
        <label>Subject: <input name="subject"></label><br/>
        <label>Message: <textarea name="message" required></textarea></label><br/>
        <label>Attachment: <input type="file" name="file1"></label><br/>
        <button type="submit">Send</button>
      </form>
    </body>
  </html>
  HTML
end

# ----------------------------
# Background cleanup thread for rate store
# ----------------------------
Thread.new do
  loop do
    sleep(RATE_LIMIT_WINDOW * 2)
    cutoff = now_epoch - (RATE_LIMIT_WINDOW * 2)
    removed = 0
    $rate_lock.synchronize do
      $rate_store.keys.each do |k|
        v = $rate_store[k]
        if v[:window_start] < cutoff
          $rate_store.delete(k)
          removed += 1
        end
      end
    end
    LOGGER.debug("Rate store cleanup removed %d entries", removed)
  end
end

# ----------------------------
# If run directly
# ----------------------------
if __FILE__ == $0
  port = (ENV['PORT'] || 4567).to_i
  LOGGER.info("Starting %s on port %d", APP_NAME, port)
  # Use Sinatra's built-in server for development; for production use Puma/Passenger and HTTPS.
  set :bind, '0.0.0.0'
  set :port, port
  run!
end