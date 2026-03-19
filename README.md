# Flight tracker backend

## Overview

backend for ios flight tracker app, restfull api that relays on fastapi and sqlite3 along with couple services from aws and aerodatabox webhook service

## Table of Contents

- [Features](#features)
- [Stack](#stack)
- [Project Structure](#project-structure)
- [Usage](#usage)
- [Deployment](#deployment)

## Features

- CRUD operations on flights and users.
- Push notifications to client app (Firebase Cloud Messaging).
- Webhook handling from aerodatabox.

## Stack

- **Language**: Python, Framework (Fastapi), ORM (sqlmodel)
- **Database**: sqlmodel
- **Environment**: Python 3.x
- **Third Party Services**: AWS: Lambda - S3 - SES - gemini-2.5-flash-lite - Aerodatabox API - Firebase

## Project Structure
```
┌── ROOT
│
├── __init__.py                     # Initializing the main package and defining lifespan
│
├── flags/                          # flags of countries as a .png format 300x300px
│
├── lambda/                         # lambda hanlder package dir
│
├── models/                         # Database / Read models
│   │
│   ├── __init__.py                 # Initializing DATABASE and define get_session
│   │
│   ├── aerodatabox.py              # Aerodatabox models
│   │
│   ├── flight.py                   # Database flight related models
│   │
│   ├── user.py                     # Database user related models
│   │
│   ├── fcm.py                      # Firebase Cloud Messaging related models
│   │
│   └── email.py                    # Database email related models
│
├── routers/                        # Fastapi Routers
│   │
│   ├── flags.py                    # Router for serving flags - maybe serve flags from nginx
│   │
│   ├── incoming_emails.py          # Router for handling incoming emails from notifications from lambda
│   │
│   ├── flights.py                  # Router for handling operations on flights
│   │
│   ├── users.py                    # Router for handling operations on users
│   │
│   └── webhook.py                  # Router for handling updates from aerodatabox webhook
│
├── services/                       # Services
│   │
│   ├── flights.py                  # Flights related service
│   │
│   ├── fcm.py                      # Firebase Cloud Messaging related service
│   │
│   └── parser/
│       │
│       ├── config.py               # Types config for Gemini service
│       │
│       └── parser.py               # Gemini related service
│
├── background_tasks.py             # Background tasks for the app
│
├── dependency.py                   # Dependency for the app
│
├── requirements.txt                # App requirements file
│
├── background_tasks.py             # Background tasks across the app
│
├── README.md                       # Project documentation
│
├── .env.example                    # environment examples
│
├── serviceAccountKey.json          # Firebase account key
│
└── main.py                         # Main app entry
```

## Usage

1. **Clone the Repository:**
    ```
    git clone https://github.com/Orino1/flight-tracker-backend
    ```
2. **Prepare aws services:**
    - Create lambda zip file with requests package included
    ``` bash
    cd lambda

    vi lambda_function.py    # edit bearer token and backend url

    python3 -m venv venv

    source venv/bin/activate
    
    pip install requests

    pip freeze > requirements.txt

    pip install --target . -r requirements.txt

    zip -r lambda_package.zip . \
    -x "*venv*" \
    -x "__pycache__/*"

    ```
    - Check the detailed setup guide [AWS Setup PDF](docs/aws-setup.pdf)
3. **Prepare local project:**
    - Pre-request: move **AppleRoot.cer** and **AuthKey_R7G482Y6WP.p8** to root folder
    
    - Python virtual environment
    ``` bash
    python3 -m venv venv

    source venv/bin/activate
    ```
    - Install the required Python dependencies:
    ```
    pip install -r requirements.txt
    ```
    - Configure environment variables:
    ```
    mv .env.example .env

    vi .env      # fill each env with its value
    ```
    - Start dev server (fetcher server on port 8001 otherwise add correct port in .env):
    ```
    # dev
    fastapi dev core/main.py --port 8000
    fastapi dev core/fetcher_service.py --port 8001

    # prod
    gunicorn -k uvicorn.workers.UvicornWorker -w 4 core.main:app
    gunicorn -k uvicorn.workers.UvicornWorker -w 4 core.fetcher_service:app # try it on linux first

    # in case above produce same infinite loop on linux use uvicorn temporary
    uvicorn core.fetcher_service:app --host 127.0.0.1 --port 8001
    ```

### Deployment ( using nginx )
- a default config would do