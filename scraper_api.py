import os
import json
import asyncio
import pandas as pd
from typing import Optional, List, Dict, Set
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException
from playwright.async_api import async_playwright, Page, ElementHandle
from urllib.parse import urljoin

# --- LLM DEPENDENCIES ---
try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
    
    # Client initialization (Assumes GEMINI_API_KEY is set in environment)
    client = genai.Client()
    GEMINI_MODEL = "gemini-2.5-flash" 
except ImportError:
    print("WARNING: Gemini client not fully initialized. Check 'pip install google-genai'.")
    client = None
    
# --- FASTAPI SETUP ---
app = FastAPI(
    title="Modular AI Agent Data Scraper (ELT Pattern)",
    description="Extracts raw data via Playwright, then uses Gemini to transform it into structured JSON.",
)

# --- 1. Pydantic Schemas (Used for API Output and LLM Input) ---

class CardDetailsSchema(BaseModel):
    """The final structured output schema."""
    card_name: Optional[str] = Field(None, description="The official name of the credit card.")
    annual_fee: Optional[str] = Field(None, description="The main annual fee, e.g., '₹499 + GST', 'NIL', '₹2,500 waived on ₹2.5 Lakh spend'.")
    milestone_duration: Optional[str] = Field(None, description="The typical period to hit the milestone (e.g., 'Annual', 'Quarterly').")
    milestone_amount: Optional[str] = Field(None, description="The spending amount required to hit the milestone (e.g., '₹1.5 Lakh').")
    milestone_reward: Optional[str] = Field(None, description="The specific reward for achieving the milestone.")
    reward_points_program: Optional[str] = Field(None, description="A concise summary of the core reward points program.")
    fees_and_charges: Optional[str] = Field(None, description="A summary of other key fees (Forex markup, cash withdrawal, etc.).")
    card_benefits: Optional[str] = Field(None, description="A consolidated summary of the card's top 3-5 main benefits.")


class CardRawData(BaseModel):
    """Intermediate model for the raw text output."""
    bank: str
    card_name: str
    url: str
    raw_text: str
    
# --- 2. Configuration and Strategy Dictionary ---

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"

# This dictionary holds the ELT Strategy for each bank.
BANK_STRATEGIES = {
    "SBI Card": {
        "list_url": "https://www.sbicard.com/en/personal/credit-cards.page",
        "list_selector": "section.card-listing.all-cards .grid.col-2",
        "name_selector": "h4",
        "link_selector": "a.learn-more-link",
        "tabs_to_click": ["View Benefits", "button.view-benefit-btn", "Fees", "Charges"] 
    },
    "Federal Bank": {
        "list_url": "https://www.federal.bank.in/credit-cards",
        "list_selector": "div.blk-deisgn.card-body",
        "name_selector": ".slider-title",
        "link_selector": "a.apply-now",
        "tabs_to_click": ["#feature-tab-1", "#feature-tab-3", "Features", "Fees & Charges"] 
    },
    "Axis Bank": {
        "list_url": "https://www.axis.bank.in/cards/credit-card",
        "list_selector": "div.card-wrapper",
        "name_selector": "h3.category-title",
        "link_selector": "a.btn-secondary",
        "tabs_to_click": ["Fees", "Charges", "a.read-more-btn"] # Includes clicks for modals
    },
    "HDFC Bank": {
        "list_url": "https://www.hdfc.bank.in/credit-cards",
        "list_selector": "div.card-wrap",
        "name_selector": "h3.card-Title",
        "link_selector": "a.btn-primary-outline",
        "tabs_to_click": ["Fees", "Charges", "Benefits", "a:has-text('Fees & Charges')"] # Guesses, likely need to be refined by user
    }
}

# --- 3. CORE ELT FUNCTIONS ---

async def get_raw_page_text(page: Page, tabs_to_click: list, max_snapshots=10) -> str:
    """Clicks tabs/buttons/modals and captures text snapshots after every interaction."""
    collected_text = []
    
    # 1. Initial Snapshot
    try:
        initial_text = await page.locator("body").inner_text()
        collected_text.append(f"--- SNAPSHOT 1: INITIAL STATE ---")
        collected_text.append(initial_text)
    except Exception: pass

    # 2. Iterate through all potential interaction triggers
    snapshot_count = 1
    for tab_identifier in tabs_to_click:
        if snapshot_count >= max_snapshots: break
        
        try:
            if any(x in tab_identifier for x in [".", "#", "button", "a:"]):
                 locator = page.locator(tab_identifier)
            else:
                 locator = page.locator(f"text=/{tab_identifier}/i")
            
            count = await locator.count()
            
            for i in range(count):
                if snapshot_count >= max_snapshots: break
                element = locator.nth(i)
                
                if await element.is_visible() and await element.is_enabled():
                    try:
                        await element.click(timeout=1500)
                        await page.wait_for_timeout(1000) 
                        
                        snapshot_count += 1
                        new_text = await page.locator("body").inner_text()
                        collected_text.append(f"--- SNAPSHOT {snapshot_count}: AFTER CLICKING {tab_identifier} #{i+1} ---")
                        collected_text.append(new_text)
                        
                        await page.keyboard.press("Escape") # Try to close modal
                        await page.wait_for_timeout(300)
                    except Exception:
                         pass
                        
        except Exception:
            pass 

    return "\n\n".join(collected_text)

def parse_with_gemini(raw_data: CardRawData) -> CardDetailsSchema:
    """Gemini-powered transformation (T in ELT)."""
    if not client:
        raise HTTPException(status_code=503, detail="LLM service not available. Check GEMINI_API_KEY.")
    
    user_prompt = f"""
    Analyze the raw text provided below for the {raw_data.card_name} credit card from {raw_data.bank}.
    Your goal is to extract the required fields into the structured JSON format provided.

    **Guidelines:**
    1. EXTRACT ONLY: Do not hallucinate. If a field is not found, return an empty string or null.
    2. CONSOLIDATE: For long fields like 'card_benefits' and 'fees_and_charges', synthesize the most important 3-5 points.
    3. BE PRECISE: Include currency symbols (₹) and exact numbers when available.

    **Raw Text Dump (Concatenation of multiple snapshots):**
    ---
    {raw_data.raw_text[:30000]} 
    ---
    """
    
    try:
        response_schema_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CardDetailsSchema,
            temperature=0.0
        )

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[user_prompt],
            config=response_schema_config
        )
        
        if response.text:
            json_data = json.loads(response.text)
            # Pydantic validation ensures integrity
            return CardDetailsSchema(**json_data)
        
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Gemini API Error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM Parsing/Validation Error: {e}")

# --- 4. DATA EXTRACTION ENDPOINT ---

@app.post("/api/v1/scrape_and_extract", response_model=List[CardDetailsSchema])
async def scrape_and_extract_cards(bank_names: List[str] = ["SBI Card", "Federal Bank", "Axis Bank", "HDFC Bank"]):
    """
    Triggers the end-to-end ELT process for the specified banks.
    """
    
    if not all(name in BANK_STRATEGIES for name in bank_names):
        available = list(BANK_STRATEGIES.keys())
        raise HTTPException(status_code=400, detail=f"Invalid bank name(s). Available banks: {available}")
    
    all_raw_data: List[CardRawData] = []
    
    # 1. Extraction (E in ELT) - The Harvester
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)

        for bank_name in bank_names:
            config = BANK_STRATEGIES[bank_name]
            
            page = await context.new_page()
            
            try:
                # Go to List Page
                await page.goto(config["list_url"], timeout=60000, wait_until="domcontentloaded")
                
                # Extract Links using the robust scraper (E in ELT)
                card_links = await default_list_scraper(page, config)

                # Visit Details & Dump Text
                detail_page = await context.new_page()
                for item in card_links:
                    await detail_page.goto(item["url"], timeout=30000, wait_until="domcontentloaded")
                    raw_text = await get_raw_page_text(detail_page, config["tabs_to_click"])
                    
                    all_raw_data.append(CardRawData(
                        bank=bank_name,
                        card_name=item["name"],
                        url=item["url"],
                        raw_text=raw_text
                    ))

                await detail_page.close()

            except Exception as e:
                print(f"Error during extraction for {bank_name}: {e}")
            finally:
                await page.close()

        await browser.close()
    
    if not all_raw_data:
        raise HTTPException(status_code=404, detail="No card data could be extracted from the source websites.")

    # 2. Transformation (T in ELT) - The LLM Parser
    # This step is synchronous and should ideally be parallelized in a real production system
    final_structured_data: List[CardDetailsSchema] = []
    
    for raw_data in all_raw_data:
        try:
            structured_model = parse_with_gemini(raw_data)
            final_structured_data.append(structured_model)
        except HTTPException as e:
            print(f"LLM failure for {raw_data.card_name}: {e.detail}")
            # Append a partial model to keep the data flow intact
            final_structured_data.append(CardDetailsSchema(
                card_name=raw_data.card_name,
                annual_fee="LLM PARSE FAILED",
                card_benefits=f"Failed to parse, check logs: {e.detail}"
            ))

    return final_structured_data