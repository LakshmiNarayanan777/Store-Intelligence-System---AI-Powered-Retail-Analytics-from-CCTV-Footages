# Deployment Guide

This repository is ready to deploy as a Dockerized API and Streamlit dashboard.

## Option 1: Render

Render can deploy both services from this repository using `render.yaml`.

1. Push this repository to GitHub.
2. Create a Render account at https://render.com.
3. Connect Render to your GitHub account.
4. Create a new service and select "Deploy from GitHub repo".
5. Render will detect `render.yaml` and create two services:
   - `store-intelligence-api`
   - `store-intelligence-dashboard`
6. If Render uses different generated hostnames, update the `API_URL` environment variable on the dashboard service to the actual API URL.

### Expected service behavior

- API service will run the FastAPI backend.
- Dashboard service will run Streamlit and use `API_URL` to fetch metrics.

## Option 2: Streamlit Community Cloud (dashboard only)

If you only want the dashboard hosted, Streamlit Cloud is the easiest option.

1. Push the repository to GitHub.
2. Sign in to Streamlit Cloud at https://streamlit.io/cloud.
3. Create a new app from the GitHub repository.
4. Set the app path to `dashboard/app.py`.
5. Set environment variable `API_URL` to the deployed API URL.

> Note: The dashboard requires the API to be running at a public URL.

## Option 3: Render API + Streamlit Cloud dashboard

1. Deploy the API service using Render.
2. Deploy the dashboard using Streamlit Cloud.
3. Configure `API_URL` on Streamlit Cloud to point to the Render API URL.

## Important

I cannot publish a live site from this environment because the machine does not have Git, deployment CLI tools, or hosting credentials.

Once you deploy on Render or Streamlit Cloud, you will receive a real hosting link like:

- `https://your-api-service.onrender.com`
- `https://your-dashboard-service.onrender.com`
- or Streamlit Cloud app URL
