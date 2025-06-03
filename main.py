from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import requests
from bs4 import BeautifulSoup
import logging
import uvicorn
import os

# Setup basic logging for diagnostics
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="InDown.io Scraper API",
    description="An API to scrape download links from indown.io for Instagram media.",
    version="1.0.0",
)

# Add CORS middleware to allow requests from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
BASE_URL = "https://indown.io"
DOWNLOAD_URL = f"{BASE_URL}/download" # The form action URL

# --- Pydantic Models ---
class InstagramRequest(BaseModel):
    instagram_url: HttpUrl # Validates that the input is a URL

class MediaItem(BaseModel):
    media_number: int
    media_type: str  # "image" or "video"
    download_links: dict[str, str]  # quality -> download_url mapping

class DownloadResponse(BaseModel):
    total_media_count: int
    media_items: list[MediaItem]

# --- Helper Functions ---
def get_initial_form_data(session: requests.Session) -> dict:
    """
    Fetches the initial page of indown.io and scrapes
    necessary hidden form field values for the POST request.
    """
    try:
        logger.info(f"Fetching initial page: {BASE_URL}")
        response = session.get(BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()
    except requests.Timeout:
        logger.error(f"Timeout while fetching initial page: {BASE_URL}")
        raise HTTPException(status_code=504, detail=f"Timeout while fetching initial page from {BASE_URL}")
    except requests.RequestException as e:
        logger.error(f"Failed to fetch initial page {BASE_URL}: {e}")
        raise HTTPException(status_code=503, detail=f"Failed to fetch initial page from {BASE_URL}: {e}")

    soup = BeautifulSoup(response.text, 'html.parser')

    form = soup.find('form', {'id': 'downloadForm'})
    if not form:
        if "Verify you are human" in response.text or "captcha" in response.text.lower():
            logger.warning(f"CAPTCHA detected on initial page load from {BASE_URL}")
            raise HTTPException(status_code=403, detail=f"CAPTCHA or human verification required by {BASE_URL} on initial page load.")
        logger.error(f"Download form (id='downloadForm') not found on {BASE_URL}")
        raise HTTPException(status_code=500, detail=f"Download form not found on {BASE_URL}. HTML structure might have changed.")

    scraped_data = {}
    required_fields = ['referer', 'locale', 'p', '_token']
    logger.info("Scraping required form fields...")
    for field_name in required_fields:
        input_tag = form.find('input', {'name': field_name})
        if not input_tag or 'value' not in input_tag.attrs:
            logger.error(f"Required field '{field_name}' not found or has no value in form on {BASE_URL}")
            raise HTTPException(status_code=500, detail=f"Required field '{field_name}' not found in form on {BASE_URL}. Page structure may have changed.")
        scraped_data[field_name] = input_tag['value']
        # logger.debug(f"Scraped {field_name}: {input_tag['value']}")

    logger.info("Successfully scraped initial form data.")
    return scraped_data

# --- API Endpoints ---
@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "message": "InDown.io Scraper API",
        "version": "1.0.0",
        "endpoints": {
            "download": "/api/v1/download_media/?instagram_url=<your_instagram_url>"
        }
    }

@app.get("/api/v1/download_media/", response_model=DownloadResponse)
async def download_media_from_instagram(instagram_url: str = Query(..., description="Instagram URL to download media from")):
    """
    Takes an Instagram URL as query parameter, scrapes indown.io, and returns potential download links.
    """
    # Validate URL format
    try:
        validated_url = HttpUrl(instagram_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid URL format: {str(e)}")
    
    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT}) # Set User-Agent for the session

        # 1. Get initial form data (like _token, p)
        try:
            form_data = get_initial_form_data(session)
        except HTTPException as e:
            raise e # Propagate HTTPException from helper
        except Exception as e:
            logger.exception("Unexpected error getting initial form data.") # Log full traceback for unexpected errors
            raise HTTPException(status_code=500, detail=f"An unexpected error occurred while getting form data: {str(e)}")

        # 2. Prepare payload for the POST request
        payload = {
            'referer': form_data.get('referer'),
            'locale': form_data.get('locale'),
            'p': form_data.get('p'),
            '_token': form_data.get('_token'),
            'link': str(validated_url) # Convert HttpUrl to string
        }

        # Ensure critical tokens are present
        if not payload['_token'] or not payload['p']:
            logger.error("Missing critical _token or p value after scraping initial form.")
            raise HTTPException(status_code=500, detail="Critical _token or p value missing after scraping. Cannot proceed.")

        # Headers for the POST request
        post_headers = {
            "Referer": BASE_URL, # Referer is important
            "Origin": BASE_URL,  # Origin is also often checked
            "Content-Type": "application/x-www-form-urlencoded",
        }

        logger.info(f"Making POST request to {DOWNLOAD_URL} for URL: {validated_url}")
        # 3. Make the POST request to get download links page
        try:
            post_response = session.post(DOWNLOAD_URL, data=payload, headers=post_headers, timeout=20)
            post_response.raise_for_status()
        except requests.Timeout:
            logger.error(f"Timeout during POST request to {DOWNLOAD_URL}")
            raise HTTPException(status_code=504, detail=f"Timeout during POST request to {DOWNLOAD_URL}")
        except requests.RequestException as e:
            error_detail = f"POST request to {DOWNLOAD_URL} failed"
            if e.response is not None:
                logger.error(f"POST request error. Status: {e.response.status_code}. Response: {e.response.text[:500]}")
                error_detail = f"{error_detail}. Status: {e.response.status_code}."
                if "Verify you are human" in e.response.text or "captcha" in e.response.text.lower():
                    error_detail = "CAPTCHA or human verification likely required by the target website."
                    raise HTTPException(status_code=403, detail=error_detail) # More specific status for CAPTCHA
                # Add more specific error checks if needed
                if e.response.status_code == 429: # Too Many Requests
                     raise HTTPException(status_code=429, detail="Too many requests made to the target website. Please try again later.")
                raise HTTPException(status_code=e.response.status_code if e.response.status_code >= 400 else 503, detail=error_detail)

            logger.error(f"POST request failed (no response object or other error): {e}")
            raise HTTPException(status_code=503, detail=f"{error_detail}: {e}")

        logger.info("POST request successful. Parsing response for download links.")
        # 4. Parse the response HTML from POST to find download links
        result_soup = BeautifulSoup(post_response.text, 'html.parser')

        # Check for common error messages displayed on the page
        error_message_tag = result_soup.find('div', class_=['alert-danger', 'alert-warning']) # Check for danger or warning alerts
        if error_message_tag:
            error_text = error_message_tag.get_text(strip=True)
            logger.warning(f"Error message found on indown.io result page: {error_text}")
            raise HTTPException(status_code=400, detail=f"Error from indown.io: {error_text}")

        categorized_media = []
        result_container = result_soup.find('div', id='result')

        if result_container:
            media_items = result_container.find_all('div', class_='col-md-4 text-center') # As per provided HTML structure
            if media_items:
                logger.info(f"Found {len(media_items)} media item blocks.")
                for item_idx, item in enumerate(media_items):
                    media_number = item_idx + 1
                    
                    # Determine media type by checking for video/image indicators
                    media_type = "image"  # default
                    if item.find('video') or 'video' in item.get_text().lower():
                        media_type = "video"
                    elif item.find('img'):
                        media_type = "image"
                    
                    btn_group = item.find('div', class_='btn-group-vertical')
                    if btn_group:
                        links_in_group = btn_group.find_all('a', href=True)
                        download_links = {}
                        
                        if links_in_group:
                            for link_idx, link_tag in enumerate(links_in_group):
                                link_text = link_tag.get_text(strip=True).lower()
                                link_url = link_tag['href']
                                
                                # Categorize by quality/type based on link text
                                if 'high' in link_text or 'hd' in link_text:
                                    quality_key = "high_quality"
                                elif 'low' in link_text or 'sd' in link_text:
                                    quality_key = "low_quality"
                                elif 'original' in link_text:
                                    quality_key = "original"
                                elif 'thumbnail' in link_text or 'thumb' in link_text:
                                    quality_key = "thumbnail"
                                else:
                                    # If no specific quality indicator, use generic naming
                                    quality_key = f"download_option_{link_idx + 1}"
                                
                                download_links[quality_key] = link_url
                                logger.info(f"Media {media_number} ({media_type}): Found {quality_key} link")
                        
                        if download_links:
                            media_item = MediaItem(
                                media_number=media_number,
                                media_type=media_type,
                                download_links=download_links
                            )
                            categorized_media.append(media_item)
                        else:
                            logger.warning(f"Media item {media_number} had btn-group-vertical but no valid download links.")
                    else:
                        logger.warning(f"Media item {media_number} did not have a 'div.btn-group-vertical'.")
            else:
                logger.warning("Result container 'div#result' found, but no 'div.col-md-4.text-center' media items within.")
        else:
            logger.warning("Result container 'div#result' not found in POST response.")
            # Check again for CAPTCHA if main content area is missing
            if "Verify you are human" in post_response.text or "captcha" in post_response.text.lower():
                logger.warning("CAPTCHA detected in POST response (result container missing).")
                raise HTTPException(status_code=403, detail="CAPTCHA or human verification likely required after POST.")


        if not categorized_media:
            logger.warning(f"No media items extracted for {validated_url}. This could be due to an invalid/private/deleted link, CAPTCHA, or website structure change.")
            # Check for more known error strings if no links and no explicit error alert was found
            response_text_lower = post_response.text.lower()
            if "private account" in response_text_lower:
                raise HTTPException(status_code=403, detail="Error from indown.io: Cannot access private account media.")
            if "link you entered is invalid" in response_text_lower:
                raise HTTPException(status_code=400, detail="Error from indown.io: The link entered is invalid or not supported.")
            if "no media found" in response_text_lower:
                 raise HTTPException(status_code=404, detail="Error from indown.io: No media found for the provided link.")

            # Fallback generic error if no specific issues detected
            raise HTTPException(status_code=404, detail="No download links found. The Instagram URL might be invalid, for a private/deleted post, a CAPTCHA was encountered, or the website's structure has changed.")

        logger.info(f"Successfully extracted and categorized {len(categorized_media)} media items for {validated_url}.")
        return DownloadResponse(
            total_media_count=len(categorized_media),
            media_items=categorized_media
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)

# To test this application:
# 1. Run the application: python main.py
# 2. Access the API documentation at `http://localhost:5000/docs`
# 3. Test endpoint: `http://localhost:5000/api/v1/download_media/?instagram_url=<your_instagram_url>`
