#coding=utf-8
import datetime
import hashlib
import requests
import json,time,os
import configparser,sys

exe_path =os.path.split(os.path.abspath(sys.argv[0]))[0]

config=configparser.ConfigParser()
config.read("config.ini", encoding="utf-8-sig") #exe_path + "\\" +
url = config.get('main', 'porturl')

roleidlist  = config.options('roleid_list')
#print(roleidlist)
role_dict = {}
for item  in roleidlist :
	p_list =  config.get("roleid_list",item)
	p_dlist = p_list.split(",")
	role_dict[p_dlist[0]] = p_dlist[1]

#print(role_dict)
def Md5Tf(data):
	checkcode = hashlib.md5(data.encode("utf-8")).hexdigest()
	#print(checkcode)
	return checkcode

def roleTchn(data):
	r_list = data.split(",")
	if len(r_list) > 0:
		rd = len(r_list)
	else:
		rd = '空'
	return rd,r_list
def get_date():
	t = datetime.datetime.now()
	dt = t.strftime('%Y-%m-%d')
	dtime = t.strftime('%Y%m%d%H%M%S')
	return dt,dtime



def userinfo(userid,url,date_format):
	mode_name = 'rest'             # 'rest' #'模块名'
	Controller_name = 'user'       # '控制器名'
	Method_name = 'getUserInfo'    # '方法名'
	encryption_key = '12345678'
	key = mode_name + Controller_name + Method_name + date_format + encryption_key

	clientId = "cmcw"
	apiToken = Md5Tf(key)
	loginId =  userid

	# print(checkcode)
	values = {
		"clientId": clientId,
		"apiToken": apiToken,
		"loginId": loginId}

	encode_json = json.dumps(values)
	# print(encode_json,type(encode_json))

	response = requests.post(url, encode_json)
	rd = response.text
	return rd

def role_to(role,dict):
	if  role in dict.keys():
		role_chn = dict[role]
	else:
		role_chn =""

	return role_chn


def role2chn(data,dict):
	tranforstr = []
	#data_plist = data.split(",")
	if isinstance(data,list):
		tranforstr = []
		for item in data:
			tranforstr.append([item,role_to(item,dict)])
		return tranforstr
	else:

		tranforstr.append([data,role_to(data,dict)])
		return tranforstr

if __name__ == '__main__':
	#url = "https://nqi.gmcc.net:20443/pro-portal/rest/user/getUserInfo"
	while True:
		date_format ,dt= get_date()
		user_id = str(input('请输入用户id:'))
		#print(type(user_id))
		#"""print(user_id,type(user_id))
		#user_id = 'nkheyuan'
		if user_id == 'q' or  user_id == 'Q':
			print("退出！！！")
			exit()
		else:
			rr = userinfo(user_id, url, date_format)
			#print(rr)
			record_dict = eval(rr)
			print(record_dict)
			if 'userInfo' in record_dict.keys():
				#print("角色:", record_dict['userInfo']['roleId'])
				data = record_dict['userInfo']['roleId']
				rd_num,r_data = roleTchn(data)
				#print(r_data,type(r_data),rd_num)
				#print(r_data.split(","))
				print("--------------------------开始-------------------------------------")
				print("用户名:", record_dict['userInfo']['userName'], "用户ID:",
					  record_dict['userInfo']['loginId'])
				print("组织名称:", record_dict['userInfo']['orgName'])
				print("角色:", role2chn(r_data, role_dict))
				print("电话:", record_dict['userInfo']['mobile'])
				print("邮箱地址:", record_dict['userInfo']['email'])

				if rd_num  == '空' :
					print("角色列表为空")
				else:
					rd_r  = role2chn(r_data,role_dict)
					#print(data)


				###########mt内容#############
				if 'mt' not in record_dict['userInfo'].keys() or len(record_dict['userInfo']['mt']) <1:
					mT = '无mt消息'
					mT_city = '无mt消息'
					mT_role = '无mt消息'


				else:
					mT = record_dict['userInfo']['mt']
					if isinstance(mT,list):
						mT_city = []
						mT_role = []
						for num in range(0,len(mT)):
							mT_city.append(record_dict['userInfo']['mt'][num]['cityName'])
							mT_role.append(record_dict['userInfo']['mt'][num]['roleId'])
					else:
						mT_city = record_dict['userInfo']['mt'][0]['cityName']
						mT_role = record_dict['userInfo']['mt'][0]['roleId']
                                                
					print(mT_role)
					for  num in range(len(mT)):
						print(mT[num])

				#print("组织名称:", record_dict['userInfo']['orgName'],"归属地市:",mT_city)
				#print("角色:", record_dict['userInfo']['roleId'],"mt权限",mT_role)
				#print("角色:", role2chn(r_data,role_dict), "mt权限", role2chn(mT_role,role_dict))
				print("---------------------------mt内容-------------------------------------")
				print("归属地市:",mT_city)
				print("mt权限:", role2chn(mT_role,role_dict))
				print("--------------------------结束----------------------------------")
			else:
				print("该用户id查询不到信息，请核实后再查询！！")

			#time.sleep(10)






