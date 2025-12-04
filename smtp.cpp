#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <map>
#include <ctime>
#include <iomanip>
#include <thread>
#include <stdexcept>
#include <algorithm>
#include <mutex>

// --- Platform-Specific Definitions for Socket and File I/O ---

#if defined(_WIN32) || defined(_WIN64)
    // Windows Headers (Winsock)
    #include <winsock2.h>
    #include <ws2tcpip.h>
    #include <direct.h> // For _mkdir
    #include <sys/stat.h> // For stat struct on Windows too
    #pragma comment(lib, "ws2_32.lib") // Link with ws2_32.lib

    // Aliases for Windows compatibility
    #define CLOSE_SOCKET closesocket
    typedef int socklen_t;
    typedef SOCKET socket_t;
    #define MKDIR(dir) _mkdir(dir)
    #define GET_ERROR() WSAGetLastError()
#else
    // POSIX (Linux/macOS) Headers
    #include <sys/socket.h>
    #include <netinet/in.h>
    #include <unistd.h> // For close()
    #include <arpa/inet.h>
    #include <sys/stat.h> // For mkdir
    
    // Aliases for POSIX compatibility
    #define CLOSE_SOCKET close
    typedef int socket_t;
    #define MKDIR(dir) mkdir(dir, 0700)
    #define GET_ERROR() (errno) // Use errno for standard POSIX errors
#endif


// Configuration
const int PORT = 2525;
const int BACKLOG = 5;
const int BUFFER_SIZE = 4096;
const std::string MAIL_SPOOL_DIR = "mail_spool";

// Mutex for safe console printing
std::mutex print_mutex;

// SMTP Session States
enum SmtpState {
    INIT,
    HELO_RECEIVED,
    MAIL_FROM_RECEIVED,
    RCPT_TO_RECEIVED,
    DATA_MODE
};

// --- Helper Functions ---

void log_message(const std::string& message) {
    std::lock_guard<std::mutex> lock(print_mutex);
    std::cout << "[LOG] " << message << std::endl;
}

void send_response(socket_t client_socket, int code, const std::string& message) {
    std::string response = std::to_string(code) + " " + message + "\r\n";
    send(client_socket, response.c_str(), response.length(), 0);
    log_message("S: " + response.substr(0, response.length() - 2)); // Log without CRLF
}

std::string get_timestamp() {
    auto t = std::time(nullptr);
    auto tm = *std::localtime(&t);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y%m%d-%H%M%S");
    return oss.str();
}

void setup_mail_spool() {
    // Create the mail spool directory if it doesn't exist
    struct stat st = {0};
    // Note: stat structure usage differs slightly between platforms, 
    // but MKDIR macro handles the function call difference.
    if (stat(MAIL_SPOOL_DIR.c_str(), &st) == -1) {
        if (MKDIR(MAIL_SPOOL_DIR.c_str()) == 0) {
            log_message("Created mail spool directory: " + MAIL_SPOOL_DIR);
        } else {
            log_message("Error creating mail spool directory: " + MAIL_SPOOL_DIR + 
                        " (Error Code: " + std::to_string(GET_ERROR()) + ")");
        }
    }
}

void save_email(const std::string& recipient, const std::string& email_data) {
    // Basic saving: recipient_timestamp.eml
    std::string filename = MAIL_SPOOL_DIR + "/" + recipient + "_" + get_timestamp() + ".eml";
    std::replace(filename.begin(), filename.end(), '@', '_');
    std::replace(filename.begin(), filename.end(), '<', '_');
    std::replace(filename.begin(), filename.end(), '>', '_');

    // 🔥 Open in binary mode to avoid newline translation on Windows
    std::ofstream outfile(filename, std::ios::binary);
    if (outfile.is_open()) {
        outfile.write(email_data.data(), email_data.size());
        outfile.close();
        log_message("Successfully saved email for " + recipient + " to " + filename);
    } else {
        log_message("ERROR: Could not open file for saving: " + filename);
    }
}

// --- Core Client Handler ---

void handle_client(socket_t client_socket, const std::string& client_ip) {
    log_message("New connection from " + client_ip);

    SmtpState state = INIT;
    std::string mail_from = "";
    std::vector<std::string> rcpt_to_list;
    std::string email_content = "";

    // 1. Initial greeting
    send_response(client_socket, 220, "SMTP Server Ready");

    char buffer[BUFFER_SIZE];
    // Use int for cross-platform compatibility with recv/send return types
    int bytes_received; 

    try {
        while (true) {
            std::string command_line;
            
            if (state == DATA_MODE) {
                log_message("Entering DATA mode...");
                std::string line;
                while ((bytes_received = recv(client_socket, buffer, 1, 0)) > 0) {
                    char c = buffer[0];
                    line += c;

                    // Line complete when we see CRLF
                    if (line.size() >= 2 && line[line.size() - 2] == '\r' && line.back() == '\n') {
                        // Remove the CRLF from the line
                        std::string trimmed_line = line.substr(0, line.size() - 2);

                        // Check for terminator "."
                        if (trimmed_line == ".") {
                            log_message("DATA termination received.");
                            // Save the email for all recipients
                            for (const auto& recipient : rcpt_to_list) {
                                save_email(recipient, email_content);
                            }
                            send_response(client_socket, 250, "OK: message accepted for delivery");
                            state = HELO_RECEIVED;     // Reset state
                            email_content.clear();     // Clear buffer
                            rcpt_to_list.clear();
                            break;                     // Exit DATA loop
                        }

                        // Dot-unstuffing: if server receives "..X", original line started with ".X"
                        if (!trimmed_line.empty() && trimmed_line[0] == '.' && trimmed_line.size() > 1) {
                            trimmed_line.erase(0, 1);  // remove one leading dot
                        }

                        // Append with CRLF to preserve email format
                        email_content += trimmed_line + "\r\n";
                        line.clear();
                    }
                }

                if (bytes_received <= 0) {
                    break;  // client disconnected
                }

                if (state != DATA_MODE) {
                    continue; // finished DATA, go read next SMTP command
                }
            }

            
            // Read next command line
            bytes_received = recv(client_socket, buffer, BUFFER_SIZE - 1, 0);
            if (bytes_received <= 0) break; // Client disconnected
            
            buffer[bytes_received] = '\0';
            command_line = buffer;
            
            // Remove CRLF
            if (command_line.length() >= 2 && command_line.substr(command_line.length() - 2) == "\r\n") {
                command_line = command_line.substr(0, command_line.length() - 2);
            }
            log_message("C: " + command_line);

            std::stringstream ss(command_line);
            std::string command;
            ss >> command;
            
            std::transform(command.begin(), command.end(), command.begin(), ::toupper);

            // --- Command Handling State Machine ---

            if (command == "EHLO" || command == "HELO") {
                std::string domain;
                ss >> domain;
                if (!domain.empty()) {
                    state = HELO_RECEIVED;
                    send_response(client_socket, 250, "Hello " + domain + ", pleased to meet you");
                    mail_from = ""; // Reset transaction
                    rcpt_to_list.clear();
                } else {
                    send_response(client_socket, 501, "Syntax error in parameters or arguments");
                }
            } 
            
            else if (command == "MAIL") {
                if (state < HELO_RECEIVED) {
                    send_response(client_socket, 503, "Bad sequence of commands (EHLO/HELO first)");
                } else {
                    std::string arg;
                    ss >> arg; // Should be "FROM:<address>"
                    if (arg.size() > 5 && arg.substr(0, 5) == "FROM:") {
                        mail_from = arg.substr(5);
                        state = MAIL_FROM_RECEIVED;
                        rcpt_to_list.clear(); // Clear recipients for new message
                        send_response(client_socket, 250, "Sender OK");
                    } else {
                        send_response(client_socket, 501, "Syntax error in parameters or arguments (MAIL FROM: expected)");
                    }
                }
            } 
            
            else if (command == "RCPT") {
                if (state < MAIL_FROM_RECEIVED) {
                    send_response(client_socket, 503, "Bad sequence of commands (MAIL FROM first)");
                } else {
                    std::string arg;
                    ss >> arg; // Should be "TO:<address>"
                    if (arg.size() > 3 && arg.substr(0, 3) == "TO:") {
                        rcpt_to_list.push_back(arg.substr(3));
                        state = RCPT_TO_RECEIVED;
                        send_response(client_socket, 250, "Recipient OK");
                    } else {
                        send_response(client_socket, 501, "Syntax error in parameters or arguments (RCPT TO: expected)");
                    }
                }
            } 
            
            else if (command == "DATA") {
                if (state < RCPT_TO_RECEIVED) {
                    send_response(client_socket, 503, "Bad sequence of commands (Need MAIL FROM and RCPT TO)");
                } else {
                    // 3. Switch to DATA mode (354)
                    send_response(client_socket, 354, "Start mail input; end with <CRLF>.<CRLF>");
                    state = DATA_MODE;
                    email_content = ""; // Clear content buffer
                }
            }
            
            else if (command == "RSET") {
                state = HELO_RECEIVED; // Reset session, keep connection open
                mail_from = "";
                rcpt_to_list.clear();
                email_content = "";
                send_response(client_socket, 250, "OK");
            }

            else if (command == "NOOP") {
                send_response(client_socket, 250, "OK");
            }
            
            else if (command == "QUIT") {
                send_response(client_socket, 221, "Service closing transmission channel");
                break; // Exit the command loop
            }
            
            else {
                send_response(client_socket, 500, "Syntax error, command unrecognized");
            }
        }
    } catch (const std::exception& e) {
        log_message("Exception in client handler: " + std::string(e.what()));
    }

    log_message("Closing connection with " + client_ip);
    CLOSE_SOCKET(client_socket); // Use platform-independent close
}

// --- Main Server Setup ---

int main() {
    
    // Windows-specific initialization
    #if defined(_WIN32) || defined(_WIN64)
    WSADATA wsaData;
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        std::cerr << "WSAStartup failed with error: " << WSAGetLastError() << std::endl;
        return 1;
    }
    #endif

    setup_mail_spool();
    
    socket_t server_fd, new_socket; // Use platform-independent socket_t
    struct sockaddr_in address;
    socklen_t addrlen = sizeof(address); // Use platform-independent socklen_t
    int opt = 1;

    // 1. Creating socket file descriptor
    if ((server_fd = socket(AF_INET, SOCK_STREAM, 0)) == (socket_t)-1) {
        log_message("socket failed (Error Code: " + std::to_string(GET_ERROR()) + ")");
        exit(EXIT_FAILURE);
    }
    
    // 2. Forcefully attach socket to the port 2525
    if (setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, (const char*)&opt, sizeof(opt))) {
        log_message("setsockopt failed (Error Code: " + std::to_string(GET_ERROR()) + ")");
        exit(EXIT_FAILURE);
    }
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = INADDR_ANY;
    address.sin_port = htons(PORT);
    
    // 3. Bind the socket
    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        log_message("bind failed (Error Code: " + std::to_string(GET_ERROR()) + ")");
        exit(EXIT_FAILURE);
    }
    
    // 4. Listen for connections
    if (listen(server_fd, BACKLOG) < 0) {
        log_message("listen failed (Error Code: " + std::to_string(GET_ERROR()) + ")");
        exit(EXIT_FAILURE);
    }

    std::cout << "=================================================" << std::endl;
    std::cout << "[SUCCESS] C++ SMTP Server listening on port " << PORT << std::endl;
    std::cout << "=================================================" << std::endl;

    try {
        while (true) {
            // 5. Accept an incoming connection
            if ((new_socket = accept(server_fd, (struct sockaddr *)&address, &addrlen)) == (socket_t)-1) {
                log_message("accept failed (Error Code: " + std::to_string(GET_ERROR()) + ")");
                continue;
            }

            // Convert IP to string for logging
            char client_ip_str[INET_ADDRSTRLEN];
            inet_ntop(AF_INET, &(address.sin_addr), client_ip_str, INET_ADDRSTRLEN);
            std::string client_ip(client_ip_str);

            // 6. Spawn a new thread to handle the client
            std::thread client_thread(handle_client, new_socket, client_ip);
            client_thread.detach(); // Detach the thread to run independently

        }
    } catch (const std::exception& e) {
        std::cerr << "[FATAL ERROR] Main loop exception: " << e.what() << std::endl;
    }

    CLOSE_SOCKET(server_fd); // Use platform-independent close

    // Windows-specific cleanup
    #if defined(_WIN32) || defined(_WIN64)
    WSACleanup();
    #endif
    
    return 0;
}
