-- One permanent mutex shared by CLI and HTTP role mutations across service instances.
CREATE TABLE role_admin_lock (id INTEGER PRIMARY KEY CHECK (id = 1));
INSERT INTO role_admin_lock (id) VALUES (1);
