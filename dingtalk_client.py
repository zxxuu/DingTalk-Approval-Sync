import os
import requests
import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

class DingTalkClient:
    def __init__(self):
        self.app_key = os.getenv('DINGTALK_CLIENT_ID', '').strip()
        self.app_secret = os.getenv('DINGTALK_CLIENT_SECRET', '').strip()
        self.access_token = None
        self.token_expires_at = 0
        
        # Debug log (masked)
        if self.app_key:
            logger.info(f"Loaded AppKey: {self.app_key[:4]}***{self.app_key[-4:]}")
        else:
            logger.error("AppKey is empty!")


    def get_access_token(self):
        """Get Access Token, refresh if expired."""
        if self.access_token and time.time() < self.token_expires_at:
            return self.access_token

        url = "https://oapi.dingtalk.com/gettoken"
        params = {
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        try:
            response = requests.get(url, params=params)
            data = response.json()
            if data.get("errcode") == 0:
                self.access_token = data["access_token"]
                # Expires in 7200s, refresh 5 mins early
                self.token_expires_at = time.time() + data["expires_in"] - 300
                logger.info("Successfully obtained AccessToken")
                return self.access_token
            else:
                logger.error(f"Failed to get AccessToken: {data}")
                raise Exception(f"DingTalk Token Error: {data}")
        except Exception as e:
            logger.error(f"Error requesting AccessToken: {e}")
            raise

    def get_department_list_ids(self, parent_dept_id=None):
        """
        Recursively fetch all department IDs.
        If parent_dept_id is None, starts from root.
        """
        url = "https://oapi.dingtalk.com/topapi/v2/department/listsub"
        token = self.get_access_token()
        params = {"access_token": token}
        payload = {}
        if parent_dept_id:
            payload["dept_id"] = parent_dept_id
        
        all_dept_ids = []
        
        try:
            response = requests.post(url, params=params, json=payload)
            data = response.json()
            if data.get("errcode") == 0:
                sub_depts = data.get("result", [])
                for dept in sub_depts:
                    dept_id = dept['dept_id']
                    all_dept_ids.append(dept_id)
                    # Recursively fetch sub-departments
                    sub_ids = self.get_department_list_ids(dept_id)
                    all_dept_ids.extend(sub_ids)
                return all_dept_ids
            else:
                logger.error(f"Failed to get departments: {data}")
                raise Exception(f"DingTalk API Error: {data}")
        except Exception as e:
            logger.error(f"Error getting departments: {e}")
            raise

    def get_dept_users(self, dept_id):
        """
        Fetch all users in a department.
        Returns a list of dicts: [{'userid': '...', 'name': '...'}]
        """
        url = "https://oapi.dingtalk.com/topapi/v2/user/list"
        token = self.get_access_token()
        params = {"access_token": token}
        payload = {
            "dept_id": dept_id,
            "cursor": 0,
            "size": 100
        }
        
        all_users = []
        while True:
            try:
                response = requests.post(url, params=params, json=payload)
                data = response.json()
                if data.get("errcode") == 0:
                    result = data.get("result", {})
                    users = result.get("list", [])
                    for u in users:
                        all_users.append({'userid': u['userid'], 'name': u['name']})
                    
                    if not result.get("has_more"):
                        break
                    payload["cursor"] = result.get("next_cursor")
                else:
                    logger.warning(f"Failed to get users for dept {dept_id}: {data}")
                    break
            except Exception as e:
                logger.error(f"Error getting users: {e}")
                raise
        return all_users

    def get_user_visible_process_codes(self, userid):
        """
        Fetch list of process codes visible to a specific user.
        """
        url = "https://oapi.dingtalk.com/topapi/process/listbyuserid"
        token = self.get_access_token()
        params = {"access_token": token}
        
        payload = {
            "userid": userid,
            "offset": 0,
            "size": 100
        }
        
        try:
            response = requests.post(url, params=params, json=payload)
            data = response.json()
            if data.get("errcode") == 0:
                result = data.get("result", {})
                process_list = result.get("process_list", [])
                return process_list
            else:
                logger.error(f"Failed to list processes for user {userid}: {data}")
                return []
        except Exception as e:
            logger.error(f"Error listing processes: {e}")
            raise

    def get_process_instance_ids(self, start_time_str, end_time_str, process_code):
        """
        Fetch process instance IDs for a given time range and process code.
        start_time_str, end_time_str: 'yyyy-MM-dd HH:mm:ss' (or compatible format)
        Returns: list of instance IDs.
        """
        # Corrected URL: processinstance/listids (no slash between process and instance)
        url = "https://oapi.dingtalk.com/topapi/processinstance/listids"
        token = self.get_access_token()
        params = {"access_token": token}
        
        # Convert to milliseconds timestamp
        def to_ts(time_str):
            dt = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
            return int(dt.timestamp() * 1000)

        start_time = to_ts(start_time_str)
        end_time = to_ts(end_time_str)
        
        all_ids = []
        cursor = 0
        size = 20
        
        while True:
            payload = {
                "process_code": process_code,
                "start_time": start_time,
                "end_time": end_time,
                "size": size,
                "cursor": cursor
            }
            
            try:
                response = requests.post(url, params=params, json=payload)
                data = response.json()
                if data.get("errcode") == 0:
                    result = data.get("result", {})
                    id_list = result.get("list", [])
                    all_ids.extend(id_list)
                    
                    if not result.get("next_cursor"):
                        break
                    cursor = result.get("next_cursor")
                else:
                    logger.error(f"Failed to get process instance IDs: {data}")
                    raise Exception(f"DingTalk API Error: {data}")
            except Exception as e:
                logger.error(f"Error getting process instance IDs: {e}")
                raise
                
        return all_ids

    def get_process_instance_detail(self, process_instance_id):
        """
        Fetch details for a single process instance.
        """
        # Corrected URL: processinstance/get
        url = "https://oapi.dingtalk.com/topapi/processinstance/get"
        token = self.get_access_token()
        params = {"access_token": token}
        
        payload = {
            "process_instance_id": process_instance_id
        }
        
        try:
            response = requests.post(url, params=params, json=payload)
            data = response.json()
            if data.get("errcode") == 0:
                return data.get("process_instance", {})
            else:
                logger.error(f"Failed to get process instance detail for {process_instance_id}: {data}")
                return None
        except Exception as e:
            logger.error(f"Error getting process instance detail: {e}")
            raise

