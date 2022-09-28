from tqdm import tqdm
from qutip import *
import numpy as np
import matplotlib.pyplot as plt
import scipy.optimize as optimize
from qutip.piqs import *
plt.rc("font",family="YouYuan",size="14") 
# plt.style.use('_mpl-gallery')#mini版的图
chi = 1
r = 0.01# np.pi / 4
N = 20
j = N//2        
ket = spin_coherent(j, np.pi / 2, 0)#初态在正上方
rho = ket2dm(ket)
Jp = jmat(j, "+")
J_ = jmat(j, "-")
Jz = jmat(j, "z")
Jy = jmat(j, "y")
Jx = jmat(j, "x")
L = np.sin(r)*Jp + np.cos(r)*J_  
H = chi*Jz**3
# H = chi*(np.cos(theta)*Jx + np.sin(theta)*np.cos(zeta)*Jy + np.sin(theta)*np.sin(zeta)*Jz)**2
n_max = 300
tlist = np.linspace(0, np.pi, n_max)
result = mesolve(H, rho, tlist, [], [])   
F = []
#T = np.arange(n_max) 
T = np.arange(0, n_max, 2) 

def f(params):# <-- for readability you may wish to assign names to the component variables
     alpha, beta = params  # print(params)  # <-- you'll see that params is a NumPy array      
     Jn = np.sin(alpha)*np.cos(beta)*Jx + np.sin(alpha)*np.sin(beta)*Jy + np.cos(alpha)*Jz        
     f = -4*variance(Jn, result.states[t])
    # 注意: 这里加了一个负号在前面，因为下面提到的方法是找最小值的。
     return f  

# 最开始随意猜最小值的位置
initial_guess = [0, 0]
for t in tqdm(T):
    # niter是指定的迭代次数
    # minimizer_kwargs是找最值时用到的参数。method是优化方法，bounds是输入的范围，我们这里规定在x,y都在0到2之间。
    out = optimize.basinhopping(f, initial_guess, niter = 10, minimizer_kwargs = dict(method = 'L-BFGS-B', bounds = ((0, np.pi), (0, 2*np.pi))))
    ff = -1*out.fun
    F.append(ff)   

label_size = 20   
fig, ax = plt.subplots()#figsize=(12,3) 
# # plt.ylim(0, 1.0)
# # plt.grid() #网格

#plt.plot(tlist, F, '-', label = 'Q', color = 'C0', lw = 1.8) 
plt.plot(tlist[::2], F, '-', label = 'Q', color = 'C0', lw = 1.8) 
plt.ylabel("QFI", fontsize = label_size, color = '#FF4500')
plt.xlabel("t", fontsize = label_size, color = '#FF4500')
plt.title('N = 10, r =  0.62', fontsize = label_size, color = '#FF4500')
plt.legend( fontsize = 0.8 * label_size, loc = 'center right') 
plt.show()
